"""The observe-decide-act loop shared by the worker and the verifier.

ToolExecutor  runs one tool call: input validation -> policy gate (block / read-only /
              human approval) -> handler -> error capture. Nothing the model asks for
              reaches a business system without passing through here.
Monitor       watches the run for failure patterns (repeated actions, error streaks,
              budget) and injects short harness notes so the model can change course.
AgentLoop     drives the model: append-only history, all tool results of a turn
              returned together, explicit handling of every stop reason.
"""

from __future__ import annotations

import json
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Callable

from playwright.sync_api import Error as PlaywrightError

from .human import ApprovalRequest
from .llm import LLM, LLMError
from .policy import Assessment, allowed_in_read_only, needs_approval
from .runtime import RunContext
from .tools.base import Tool, ToolError, ToolResult, validate_input


def _short(value: Any, limit: int = 300) -> str:
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    return text if len(text) <= limit else text[:limit] + "..."


class ToolExecutor:
    def __init__(self, ctx: RunContext, tools: list[Tool]) -> None:
        self.ctx = ctx
        self.tools = {t.name: t for t in tools}
        self.writes: list[dict] = []

    def schemas(self) -> list[dict]:
        return [t.schema() for t in self.tools.values()]

    def execute(self, name: str, args: dict) -> ToolResult:
        ctx = self.ctx
        tool = self.tools.get(name)
        if tool is None:
            return ToolResult(f"Unknown tool '{name}'. Available: {', '.join(self.tools)}", is_error=True)
        if problem := validate_input(tool.input_schema, args):
            return ToolResult(f"Invalid input for {name}: {problem}", is_error=True)

        assessment: Assessment | None = None
        if tool.assess is not None:
            try:
                assessment = tool.assess(args, ctx)
            except ToolError as e:
                return ToolResult(str(e), is_error=True)
            if denial := self._gate(name, assessment):
                return denial

        try:
            result = tool.handler(args, ctx)
        except ToolError as e:
            result = ToolResult(str(e), is_error=True)
        except PlaywrightError as e:
            result = ToolResult(f"Browser error: {str(e).splitlines()[0]}", is_error=True)
        except Exception as e:  # keep the run alive; the model can recover or report
            result = ToolResult(f"Unexpected {type(e).__name__}: {e}", is_error=True)

        if assessment and assessment.at_least("write") and not result.is_error:
            self.writes.append({"step": ctx.step, "action": assessment.summary, "risk": assessment.risk,
                                "result_url": result.data.get("url"), "http_status": result.data.get("status")})
        return result

    def _gate(self, name: str, a: Assessment) -> ToolResult | None:
        ctx = self.ctx
        ctx.trace.emit("policy", tool=name, risk=a.risk, action=a.summary, reason=a.reason)
        if a.risk == "blocked":
            return ToolResult(f"Blocked by policy: {a.summary} ({a.reason}).", is_error=True)
        if ctx.read_only and not allowed_in_read_only(a):
            return ToolResult(f"Refused: read-only session cannot {a.summary}.", is_error=True)
        if not needs_approval(a, ctx.config.approval_mode):
            return None
        if ctx.config.approval_mode == "never-ask":
            ctx.trace.emit("approval_result", approved=False, note="never-ask mode", action=a.summary)
            return ToolResult(f"Refused by policy: '{a.summary}' needs human approval and this run is unattended. "
                              "Do not attempt it another way; report it to the user instead.", is_error=True)
        request = ApprovalRequest(action=a.summary, risk=a.risk, reason=a.reason or "policy requires approval")
        ctx.trace.emit("approval_request", action=request.action, risk=request.risk, reason=request.reason,
                       origin="policy")
        decision = ctx.human.approve(request)
        ctx.trace.emit("approval_result", approved=decision.approved, note=decision.note, action=a.summary)
        ctx.interactions.append({"kind": "approval", "action": a.summary, "approved": decision.approved,
                                 "note": decision.note, "origin": "policy"})
        if decision.approved:
            return None
        note = f" The user said: {decision.note}" if decision.note else ""
        return ToolResult(f"The user denied approval for: {a.summary}.{note} The action was NOT performed. "
                          "Do not try to achieve it another way.", is_error=True)


@dataclass
class Monitor:
    """Detects failure patterns and produces corrective harness notes."""

    max_steps: int
    deadline: float
    error_streak: int = 0
    repeats: Counter = field(default_factory=Counter)
    warned_budget: bool = False

    def after_turn(self, step: int, calls: list[tuple[str, dict, str]], results: list[ToolResult]) -> list[str]:
        """`calls` holds (tool, input, page URL the call was made on); refs like e4 repeat across pages."""
        notes = []
        if results and all(r.is_error for r in results):
            self.error_streak += 1
        elif results:
            self.error_streak = 0
        if self.error_streak >= 3:
            notes.append("The last several actions all failed. Step back: take a fresh snapshot, re-read the error "
                         "messages, and try a different approach. If the obstacle can't be overcome, finish with "
                         "status 'blocked' and explain why.")
            self.error_streak = 0
        for name, args, page in calls:
            if name in {"browser_snapshot", "update_plan", "remember"}:
                continue
            sig = name + json.dumps(args, sort_keys=True) + page
            self.repeats[sig] += 1
            if self.repeats[sig] == 3:
                notes.append(f"You have now called {name} with identical input 3 times. If it isn't producing "
                             "progress, change approach instead of repeating it.")
        remaining = self.max_steps - step - 1
        if not self.warned_budget and (remaining <= 5 or time.monotonic() > self.deadline - 120):
            self.warned_budget = True
            notes.append(f"Budget warning: about {max(remaining, 1)} turns left. Prioritise checking the end state "
                         "and call finish with an accurate status.")
        return notes


@dataclass
class LoopOutcome:
    terminal: ToolResult | None
    reason: str  # finished | step_budget | time_budget | refusal | llm_error | no_progress
    steps: int
    error: str = ""


TerminalHook = Callable[[str, dict, ToolResult], ToolResult]


class AgentLoop:
    def __init__(self, ctx: RunContext, llm: LLM, system: str, executor: ToolExecutor, *, effort: str,
                 max_steps: int, max_minutes: float, actor: str = "worker",
                 on_terminal: TerminalHook | None = None) -> None:
        self.ctx, self.llm, self.system, self.executor = ctx, llm, system, executor
        self.effort, self.max_steps, self.actor = effort, max_steps, actor
        self.deadline = time.monotonic() + max_minutes * 60  # monotonic: sleep doesn't eat the budget
        self.on_terminal = on_terminal
        self.messages: list[dict] = []

    def _emit(self, type_: str, **data: Any) -> None:
        self.ctx.trace.emit(type_, actor=self.actor, step=self.ctx.step, **data)

    def run(self, first_message: str) -> LoopOutcome:
        self.messages = [{"role": "user", "content": first_message}]
        monitor = Monitor(self.max_steps, self.deadline)
        idle_turns = 0
        page_url = ""  # URL of the page the next browser action acts on
        tools = self.executor.schemas()
        for step in range(self.max_steps):
            self.ctx.step = step + 1
            if time.monotonic() > self.deadline:
                return LoopOutcome(None, "time_budget", step)
            try:
                response = self.llm.create(system=self.system, messages=self.messages, tools=tools,
                                           effort=self.effort)
            except LLMError as e:
                self._emit("error", message=str(e))
                return LoopOutcome(None, "llm_error", step, str(e))
            self.messages.append({"role": "assistant", "content": response.content})  # append-only history
            self._trace_response(response)

            if response.stop_reason == "refusal":
                details = getattr(response, "stop_details", None)
                msg = f"The model declined to continue ({getattr(details, 'category', None) or 'no category'})."
                self._emit("error", message=msg)
                return LoopOutcome(None, "refusal", step + 1, msg)

            calls = [b for b in response.content if b.type == "tool_use"]
            if not calls:
                idle_turns += 1
                if idle_turns > 2:
                    return LoopOutcome(None, "no_progress", step + 1, "The model stopped calling tools.")
                self.messages.append({"role": "user", "content": (
                    "[harness] You ended your turn without using a tool. Nobody is reading intermediate messages. "
                    "Continue the task with tools, or call finish if you are done or blocked.")})
                continue
            idle_turns = 0

            results, blocks, terminal, called = [], [], None, []
            truncated = response.stop_reason == "max_tokens"
            for call in calls:
                if truncated:
                    result = ToolResult("Not executed: your response was cut off. Use shorter inputs.", is_error=True)
                else:
                    self._emit("tool_call", tool=call.name, input=call.input, call_id=call.id)
                    called.append((call.name, call.input, page_url))
                    result = self.executor.execute(call.name, call.input)
                    page_url = result.data.get("url") or page_url
                    if result.terminal and self.on_terminal:
                        result = self.on_terminal(call.name, call.input, result)
                self._emit("tool_result", tool=call.name, call_id=call.id, is_error=result.is_error,
                           output=_short(result.text(), 1500), data=result.data)
                results.append(result)
                blocks.append({"type": "tool_result", "tool_use_id": call.id, "content": result.content,
                               "is_error": result.is_error})
                if result.terminal:
                    terminal = result
            if terminal:
                return LoopOutcome(terminal, "finished", step + 1)

            notes = monitor.after_turn(step, called, results)
            for note in notes:
                self._emit("harness_note", note=note)
            blocks += [{"type": "text", "text": f"[harness] {n}"} for n in notes]
            self.messages.append({"role": "user", "content": blocks})
        return LoopOutcome(None, "step_budget", self.max_steps)

    def _trace_response(self, response: Any) -> None:
        for block in response.content:
            if block.type == "thinking" and getattr(block, "thinking", ""):
                self._emit("thinking", text=block.thinking)
            elif block.type == "text" and block.text.strip():
                self._emit("assistant_text", text=block.text)
            elif block.type == "fallback":
                self._emit("harness_note", note="Model fallback engaged for this turn.")
        usage = getattr(self.llm, "usage", None)
        if usage is not None:
            self._emit("llm_usage", calls=usage.calls, cost_usd=round(usage.cost_usd, 4),
                       stop_reason=response.stop_reason)

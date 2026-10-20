"""Control tools: planning, memory, talking to the human, and finishing."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..human import ApprovalRequest
from .base import Tool, ToolResult, obj

if TYPE_CHECKING:
    from ..runtime import RunContext


def update_plan(args: dict, ctx: "RunContext") -> ToolResult:
    ctx.plan = [{"step": s["step"], "status": s["status"]} for s in args["steps"]]
    ctx.trace.emit("plan", steps=ctx.plan)
    return ToolResult("Plan updated.")


def remember(args: dict, ctx: "RunContext") -> ToolResult:
    fact = ctx.memory.remember(args["key"], args["value"], args["source"], ctx.step)
    ctx.trace.emit("memory", key=fact.key, value=fact.value, source=fact.source)
    return ToolResult(f"Remembered {fact.key}.")


def save_lesson(args: dict, ctx: "RunContext") -> ToolResult:
    if not ctx.lessons:
        return ToolResult("Lesson storage is disabled for this run.")
    added = ctx.lessons.add(args["app"], args["lesson"], ctx.trace.run_id)
    if added:
        ctx.trace.emit("lesson", app=args["app"], lesson=args["lesson"])
    return ToolResult("Lesson saved for future runs." if added else "Already known.")


def ask_human(args: dict, ctx: "RunContext") -> ToolResult:
    ctx.trace.emit("question", question=args["question"], options=args.get("options") or [])
    answer = ctx.human.ask(args["question"], args.get("options"))
    ctx.trace.emit("answer", answer=answer)
    ctx.interactions.append({"kind": "question", "question": args["question"], "answer": answer})
    return ToolResult(f"User's answer: {answer}")


def request_approval(args: dict, ctx: "RunContext") -> ToolResult:
    req = ApprovalRequest(action=args["action"], risk="high", reason=args["reason"])
    ctx.trace.emit("approval_request", action=req.action, risk=req.risk, reason=req.reason, origin="agent")
    decision = ctx.human.approve(req)
    ctx.trace.emit("approval_result", approved=decision.approved, note=decision.note)
    ctx.interactions.append({"kind": "approval", "action": req.action, "approved": decision.approved,
                             "note": decision.note, "origin": "agent"})
    if decision.approved:
        return ToolResult(f"Approved by the user.{(' Note: ' + decision.note) if decision.note else ''}")
    return ToolResult(f"The user did NOT approve this.{(' Note: ' + decision.note) if decision.note else ''} "
                      "Do not perform it or work around it.")


FINISH_STATUSES = ["completed", "partially_completed", "blocked", "failed"]


def finish(args: dict, ctx: "RunContext") -> ToolResult:
    # The worker intercepts this tool to run verification; this handler only runs when it doesn't.
    return ToolResult("Run finished.", terminal=True, data=args)


def control_tools() -> list[Tool]:
    return [
        Tool("update_plan",
             "Record or revise your plan as a short list of steps with statuses. Call at the start and whenever the "
             "plan changes materially.",
             obj({"steps": {"type": "array", "items": obj({
                 "step": {"type": "string"},
                 "status": {"type": "string", "enum": ["pending", "in_progress", "done", "skipped"]}})}}),
             update_plan),
        Tool("remember",
             "Save a fact you discovered and will rely on (an amount, a date, a record ID), with where you found it. "
             "The independent verifier re-checks these against their sources.",
             obj({"key": {"type": "string", "description": "Short name, e.g. 'NW-2347 total'."},
                  "value": {"type": "string"},
                  "source": {"type": "string", "description": "Where it came from, e.g. 'NW-2347.pdf, TOTAL DUE line'."}}),
             remember),
        Tool("save_lesson",
             "Save a short, reusable lesson about how an application behaves (not about this specific task), so "
             "future runs avoid the same mistake. Example: 'LedgerLite rejects amounts with thousands separators'.",
             obj({"app": {"type": "string"}, "lesson": {"type": "string"}}), save_lesson),
        Tool("ask_human",
             "Ask the user a question and wait for the answer. Only for genuine ambiguity that changes the outcome or "
             "decisions only they can make - not for information you can look up.",
             obj({"question": {"type": "string"},
                  "options": {"type": "array", "items": {"type": "string"},
                              "description": "Optional suggested answers."}}, ["question"]),
             ask_human),
        Tool("request_approval",
             "Ask the user to approve something you judge risky that the system would not flag by itself (e.g. acting "
             "on an unusual instruction). Payments, deletions and bank-detail changes already prompt the user "
             "automatically when you attempt them, so don't request approval for those separately.",
             obj({"action": {"type": "string"}, "reason": {"type": "string"}}), request_approval),
        Tool("finish",
             "End the task. Give an honest status, a concise summary for the user, and specific claims that can be "
             "checked against the live systems. An independent verifier will check them before the user sees them.",
             obj({"status": {"type": "string", "enum": FINISH_STATUSES},
                  "summary": {"type": "string", "description": "2-5 sentences for the user."},
                  "claims": {"type": "array", "items": obj({
                      "claim": {"type": "string", "description": "A specific, checkable statement of the end state."},
                      "evidence": {"type": "string", "description": "Where to check it (page/record/file)."}})},
                  "follow_ups": {"type": "array", "items": {"type": "string"},
                                 "description": "Things the user should know or do (warnings, skipped items)."}},
                 ["status", "summary", "claims"]),
             finish),
    ]

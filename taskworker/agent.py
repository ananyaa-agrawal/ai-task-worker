"""The worker: takes a natural-language task and runs it to a verified result."""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .config import Config
from .human import HumanChannel
from .llm import LLM, Usage, make_llm
from .loop import AgentLoop, LoopOutcome, ToolExecutor
from .memory import LessonStore
from .prompts import worker_system
from .runtime import RunContext
from .tools.base import ToolResult
from .tools.browser import BrowserHost, browser_tools
from .tools.control import control_tools
from .tools.files import file_tools
from .trace import Listener, Trace, new_run_dir
from .verifier import Verdict, Verifier

VERIFIED_STATUSES = {"completed", "partially_completed"}


@dataclass
class RunResult:
    run_id: str
    task: str
    outcome: str  # verified | completed_unverified | verification_failed | partial | blocked | failed | incomplete
    agent_status: str
    summary: str
    claims: list[dict] = field(default_factory=list)
    follow_ups: list[str] = field(default_factory=list)
    verification: dict | None = None
    facts: list[dict] = field(default_factory=list)
    plan: list[dict] = field(default_factory=list)
    interactions: list[dict] = field(default_factory=list)
    writes: list[dict] = field(default_factory=list)
    repair_rounds: int = 0
    steps: int = 0
    duration_s: float = 0.0
    usage: dict = field(default_factory=dict)
    run_dir: str = ""
    error: str = ""


class Worker:
    def __init__(self, config: Config, human: HumanChannel, llm: LLM | None = None,
                 verifier_llm: LLM | None = None, listeners: list[Listener] | None = None) -> None:
        self.config = config
        self.human = human
        self.llm = llm or make_llm(config)
        self.verifier_llm = verifier_llm or (make_llm(config) if llm is None else llm)
        self.listeners = listeners or []
        self.trace: Trace | None = None

    def trace_emit(self, type_: str, **data) -> None:
        """Emit into the current run's trace (used by UIs that interact with a running worker)."""
        if self.trace is not None:
            self.trace.emit(type_, **data)

    def run(self, task: str, run_dir: Path | None = None) -> RunResult:
        started = time.time()
        trace = self.trace = Trace(run_dir or new_run_dir(self.config.runs_dir), self.listeners)
        host = BrowserHost(headless=self.config.headless)
        lessons = LessonStore(self.config.lessons_path) if self.config.use_lessons else None
        ctx = RunContext(config=self.config, trace=trace, human=self.human, host=host, lessons=lessons,
                         secrets=self.config.load_secrets())
        verifier = Verifier(self.config, self.verifier_llm, host, trace, ctx.secrets)
        executor = ToolExecutor(ctx, browser_tools() + file_tools() + control_tools())
        state = {"repairs": 0, "verdict": None, "report": None}

        def on_finish(name: str, args: dict, result: ToolResult) -> ToolResult:
            state["report"] = args
            trace.emit("finish_attempt", status=args["status"], summary=args["summary"],
                       claims=args.get("claims", []), follow_ups=args.get("follow_ups", []))
            nothing_to_check = args["status"] not in VERIFIED_STATUSES and not executor.writes
            if not self.config.verify or nothing_to_check:
                return result
            verdict = verifier.verify(task, args, ctx.memory.as_list(), executor.writes)
            state["verdict"] = verdict
            if verdict.verdict != "fail" or state["repairs"] >= self.config.max_repair_rounds:
                return result
            state["repairs"] += 1
            findings = "\n".join(f"- {c.get('check')}: expected {c.get('expected')!r}, observed {c.get('observed')!r}"
                                 for c in verdict.checks if not c.get("passed"))
            trace.emit("repair_round", round=state["repairs"], findings=verdict.summary)
            return ToolResult(
                "An independent check of the live systems did NOT confirm your result.\n"
                f"Verifier summary: {verdict.summary}\nFailed checks:\n{findings or '(none itemised)'}\n\n"
                "Investigate and fix what you can. Check the current state before repeating any write so you "
                "don't create duplicates. Then call finish again with an accurate status and claims.",
                is_error=True)

        system = worker_system(self.config, lessons.render() if lessons else "(disabled for this run)")
        loop = AgentLoop(ctx, self.llm, system, executor, effort=self.config.effort,
                         max_steps=self.config.max_steps, max_minutes=self.config.max_minutes,
                         on_terminal=on_finish)
        trace.emit("run_started", task=task, model=getattr(self.llm, "model", "?"),
                   approval_mode=self.config.approval_mode, verify=self.config.verify)
        try:
            outcome = loop.run(f"Task from your colleague:\n{task}")
        except Exception as e:  # infrastructure failure (browser crashed, etc.)
            trace.emit("error", message=f"{type(e).__name__}: {e}")
            outcome = LoopOutcome(None, "crash", ctx.step, f"{type(e).__name__}: {e}")
        finally:
            ctx.close_browser()
            host.close()

        result = self._result(task, trace, ctx, executor, outcome, state, started)
        self._write_outputs(trace.run_dir, result)
        trace.emit("run_finished", result=asdict(result))
        trace.close()
        return result

    def _result(self, task, trace, ctx, executor, outcome: LoopOutcome, state, started) -> RunResult:
        report = state["report"] if outcome.reason == "finished" else None
        verdict: Verdict | None = state["verdict"]
        usage = Usage()
        usage.merge(self.llm.usage)
        if self.verifier_llm is not self.llm:
            usage.merge(self.verifier_llm.usage)
        if report is None:
            agent_status, outcome_label = "incomplete", "incomplete"
            summary = {"step_budget": "Stopped: step budget exhausted before the task was finished.",
                       "time_budget": "Stopped: time budget exhausted before the task was finished.",
                       }.get(outcome.reason, f"Stopped: {outcome.reason}. {outcome.error}".strip())
        else:
            agent_status, summary = report["status"], report["summary"]
            outcome_label = self._outcome_label(agent_status, verdict)
        return RunResult(
            run_id=trace.run_id, task=task, outcome=outcome_label, agent_status=agent_status, summary=summary,
            claims=(report or {}).get("claims", []), follow_ups=(report or {}).get("follow_ups", []),
            verification=verdict.as_dict() if verdict else None, facts=ctx.memory.as_list(), plan=ctx.plan,
            interactions=ctx.interactions, writes=executor.writes, repair_rounds=state["repairs"],
            steps=outcome.steps, duration_s=round(time.time() - started, 1), usage=usage.as_dict(),
            run_dir=str(trace.run_dir), error=outcome.error)

    @staticmethod
    def _outcome_label(agent_status: str, verdict: Verdict | None) -> str:
        if agent_status in {"blocked", "failed"}:
            return agent_status if not verdict or verdict.verdict != "fail" else "verification_failed"
        if verdict is None:
            return "completed_unverified" if agent_status == "completed" else "partial"
        if verdict.verdict == "pass":
            return "verified" if agent_status == "completed" else "partial"
        if verdict.verdict == "fail":
            return "verification_failed"
        return "completed_unverified"

    def _write_outputs(self, run_dir: Path, r: RunResult) -> None:
        (run_dir / "result.json").write_text(json.dumps(asdict(r), indent=2, default=str))
        lines = [f"# Run {r.run_id}", "", f"**Task:** {r.task}", "",
                 f"**Outcome:** `{r.outcome}` (worker status: {r.agent_status}, repair rounds: {r.repair_rounds})", "",
                 "## Summary", r.summary, ""]
        if r.claims:
            lines += ["## Claims"] + [f"- {c.get('claim')} _(evidence: {c.get('evidence', '-')})_" for c in r.claims] + [""]
        if r.follow_ups:
            lines += ["## Follow-ups"] + [f"- {f}" for f in r.follow_ups] + [""]
        if r.verification:
            v = r.verification
            lines += ["## Independent verification", f"**Verdict:** {v['verdict']} - {v['summary']}", ""]
            lines += [f"- {'PASS' if c.get('passed') else 'FAIL'}: {c.get('check')} (expected: {c.get('expected')}; "
                      f"observed: {c.get('observed')})" for c in v.get("checks", [])]
            lines += [""] + [f"![evidence]({p})" for p in v.get("evidence", [])] + [""]
        if r.facts:
            lines += ["## Facts used"] + [f"- {f['key']}: {f['value']} _(source: {f['source']})_" for f in r.facts] + [""]
        if r.writes:
            lines += ["## Changes made"] + [f"- step {w['step']}: {w['action']} -> {w.get('result_url')}" for w in r.writes] + [""]
        if r.interactions:
            lines += ["## Human interactions"] + [f"- {json.dumps(i)}" for i in r.interactions] + [""]
        u = r.usage
        lines += ["## Cost", f"{u.get('calls', 0)} model calls, {u.get('input_tokens', 0)} input + "
                  f"{u.get('cache_read_tokens', 0)} cached + {u.get('output_tokens', 0)} output tokens, "
                  f"~${u.get('cost_usd', 0)}; {r.steps} steps in {r.duration_s}s."]
        (run_dir / "report.md").write_text("\n".join(lines) + "\n")

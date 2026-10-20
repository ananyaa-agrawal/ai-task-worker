"""Independent verification of the worker's result.

A fresh model context with no access to the worker's reasoning, a separate read-only browser
session, and one job: decide from primary sources whether the user's outcome was achieved.
Separating "doing" from "checking" catches the classic agent failure of declaring success
because the last page looked fine.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from .config import Config
from .human import UnattendedHuman
from .llm import LLM
from .loop import AgentLoop, ToolExecutor
from .prompts import verifier_system
from .runtime import RunContext
from .tools.base import Tool, ToolResult, obj
from .tools.browser import BrowserHost, browser_tools
from .tools.files import file_tools
from .trace import Trace

VERDICTS = ["pass", "fail", "inconclusive"]


@dataclass
class Verdict:
    verdict: str
    summary: str
    checks: list[dict] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)  # screenshot paths inside the run dir

    def as_dict(self) -> dict:
        return self.__dict__.copy()


def _submit_verdict(args: dict, ctx: RunContext) -> ToolResult:
    return ToolResult("Verdict recorded.", terminal=True, data=args)


SUBMIT_VERDICT = Tool(
    "submit_verdict",
    "Record your verdict with the individual checks you performed.",
    obj({"verdict": {"type": "string", "enum": VERDICTS},
         "summary": {"type": "string", "description": "1-3 sentences explaining the verdict."},
         "checks": {"type": "array", "items": obj({
             "check": {"type": "string"},
             "expected": {"type": "string"},
             "observed": {"type": "string"},
             "passed": {"type": "boolean"}})}}),
    _submit_verdict,
)


class Verifier:
    def __init__(self, config: Config, llm: LLM, host: BrowserHost, trace: Trace, secrets: dict[str, str]) -> None:
        self.config, self.llm, self.host, self.trace, self.secrets = config, llm, host, trace, secrets
        self.rounds = 0

    def verify(self, task: str, report: dict, facts: list[dict], writes: list[dict]) -> Verdict:
        self.rounds += 1
        ctx = RunContext(config=self.config, trace=self.trace, human=UnattendedHuman(), host=self.host,
                         session_label=f"verifier{self.rounds}", read_only=True, secrets=self.secrets)
        tools = browser_tools() + [t for t in file_tools() if t.name != "write_file"] + [SUBMIT_VERDICT]
        executor = ToolExecutor(ctx, tools)
        loop = AgentLoop(ctx, self.llm, verifier_system(self.config), executor,
                         effort=self.config.verifier_effort, max_steps=24, max_minutes=8, actor="verifier")
        self.trace.emit("verification_started", round=self.rounds)
        message = (
            f"# The user's task\n{task}\n\n"
            f"# The worker's report\n{json.dumps(report, indent=2)}\n\n"
            f"# Facts the worker recorded (with claimed sources)\n{json.dumps(facts, indent=2) if facts else '(none)'}\n\n"
            f"# State-changing actions the worker performed\n"
            f"{json.dumps(writes, indent=2) if writes else '(none recorded)'}\n\n"
            "Verify the outcome now."
        )
        try:
            outcome = loop.run(message)
        finally:
            ctx.close_browser()
        shots = [e for e in self._screenshots(ctx.session_label)]
        if outcome.terminal is None:
            verdict = Verdict("inconclusive", f"Verification did not complete ({outcome.reason}). {outcome.error}".strip(),
                              evidence=shots)
        else:
            d = outcome.terminal.data
            verdict = Verdict(d["verdict"], d["summary"], d.get("checks", []), shots)
        self.trace.emit("verification_result", round=self.rounds, **verdict.as_dict())
        return verdict

    def _screenshots(self, label: str) -> list[str]:
        shots = sorted((self.trace.run_dir / "shots").glob(f"*-{label}-*.jpg"))
        return [str(p.relative_to(self.trace.run_dir)) for p in shots][-3:]

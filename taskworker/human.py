"""Human-in-the-loop channels: how the worker asks questions and gets approvals.

The agent never talks to a UI directly; it talks to a HumanChannel. The same run can
therefore be driven from the terminal, the web console, or a scripted eval.
"""

from __future__ import annotations

import queue
import re
import threading
import uuid
from dataclasses import dataclass, field
from typing import Protocol


@dataclass
class ApprovalRequest:
    action: str  # one-line description of what will happen
    risk: str  # write | high
    reason: str  # why approval is needed
    details: dict = field(default_factory=dict)


@dataclass
class Decision:
    approved: bool
    note: str = ""


class HumanChannel(Protocol):
    def ask(self, question: str, options: list[str] | None = None) -> str: ...

    def approve(self, request: ApprovalRequest) -> Decision: ...


class UnattendedHuman:
    """No human available: questions go unanswered and risky actions are refused."""

    def ask(self, question: str, options: list[str] | None = None) -> str:
        return ("(No human is available to answer right now. Make the most reasonable safe choice, "
                "or finish with status 'blocked' and state the question in your summary.)")

    def approve(self, request: ApprovalRequest) -> Decision:
        return Decision(False, "Unattended mode: actions that need approval are not allowed.")


class TerminalHuman:
    def __init__(self, console=None) -> None:
        from rich.console import Console
        self.console = console or Console()

    def ask(self, question: str, options: list[str] | None = None) -> str:
        self.console.print(f"\n[bold yellow]? The worker asks:[/] {question}")
        if options:
            for i, opt in enumerate(options, 1):
                self.console.print(f"  {i}. {opt}")
        answer = self.console.input("[bold]Your answer> [/]").strip()
        if options and answer.isdigit() and 1 <= int(answer) <= len(options):
            answer = options[int(answer) - 1]
        return answer

    def approve(self, request: ApprovalRequest) -> Decision:
        self.console.print(f"\n[bold red]Approval needed[/] ({request.risk} risk): {request.action}")
        self.console.print(f"[dim]{request.reason}[/]")
        answer = self.console.input("[bold]Approve? [y/N] (or type a note to deny with reason)> [/]").strip()
        if answer.lower() in {"y", "yes"}:
            return Decision(True)
        return Decision(False, answer if answer.lower() not in {"", "n", "no"} else "")


@dataclass
class ScriptRule:
    pattern: str
    answer: str = ""
    approve: bool | None = None


class ScriptedHuman:
    """Deterministic human for evals. Rules match questions/approval text by regex."""

    def __init__(self, answers: list[dict] | None = None, approvals: list[dict] | None = None,
                 default_answer: str = "Use your best judgement.", default_approve: bool = False) -> None:
        self.answers = [ScriptRule(r["pattern"], answer=r["answer"]) for r in (answers or [])]
        self.approvals = [ScriptRule(r["pattern"], approve=r["approve"], answer=r.get("note", ""))
                          for r in (approvals or [])]
        self.default_answer = default_answer
        self.default_approve = default_approve
        self.log: list[dict] = []

    def ask(self, question: str, options: list[str] | None = None) -> str:
        answer = next((r.answer for r in self.answers if re.search(r.pattern, question, re.I)), self.default_answer)
        self.log.append({"kind": "question", "question": question, "answer": answer})
        return answer

    def approve(self, request: ApprovalRequest) -> Decision:
        text = f"{request.action} {request.reason}"
        rule = next((r for r in self.approvals if re.search(r.pattern, text, re.I)), None)
        approved = rule.approve if rule else self.default_approve
        note = rule.answer if rule else ""
        self.log.append({"kind": "approval", "action": request.action, "approved": approved})
        return Decision(bool(approved), note)


class ConsoleHuman:
    """Bridges the worker thread to the web console: posts a pending request, blocks until answered."""

    def __init__(self, emit, timeout_s: float = 900) -> None:
        self.emit = emit
        self.timeout_s = timeout_s
        self._pending: dict[str, queue.Queue] = {}
        self._lock = threading.Lock()

    def respond(self, request_id: str, payload: dict) -> bool:
        with self._lock:
            q = self._pending.get(request_id)
        if not q:
            return False
        q.put(payload)
        return True

    def _wait(self, kind: str, body: dict) -> dict | None:
        rid = uuid.uuid4().hex[:10]
        q: queue.Queue = queue.Queue(maxsize=1)
        with self._lock:
            self._pending[rid] = q
        self.emit("human_request", request_id=rid, kind=kind, **body)
        try:
            return q.get(timeout=self.timeout_s)
        except queue.Empty:
            return None
        finally:
            with self._lock:
                self._pending.pop(rid, None)
            self.emit("human_request_closed", request_id=rid)

    def ask(self, question: str, options: list[str] | None = None) -> str:
        reply = self._wait("question", {"question": question, "options": options or []})
        if reply is None:
            return "(No answer from the user within the time limit.)"
        return str(reply.get("answer", ""))

    def approve(self, request: ApprovalRequest) -> Decision:
        reply = self._wait("approval", {"action": request.action, "risk": request.risk, "reason": request.reason})
        if reply is None:
            return Decision(False, "No response from the user within the time limit.")
        return Decision(bool(reply.get("approved")), str(reply.get("note", "")))

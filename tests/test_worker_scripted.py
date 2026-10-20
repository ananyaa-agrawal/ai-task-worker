"""End-to-end tests of the harness with a scripted model: real sandbox, real browser, real policy gate.

These prove the plumbing (tools, refs, downloads, PDF reading, secrets, approvals, read-only
verification, failure notes) independently of model quality, at zero API cost.
"""

from typing import Callable

from conftest import ref

from taskworker.agent import Worker
from taskworker.human import ScriptedHuman
from taskworker.llm import ScriptedLLM, text_block, tool_call
from taskworker.sandbox import server

Step = Callable[[str], list]


def scripted(steps: list[Step]) -> ScriptedLLM:
    def policy(i: int, results: list[dict]) -> list:
        last = ""
        if results:
            c = results[-1]["content"]
            last = c if isinstance(c, str) else "\n".join(b.get("text", "") for b in c)
        if i >= len(steps):
            return [text_block("out of script")]
        return steps[i](last)
    return ScriptedLLM(policy)


ENTER_NW_2347: list[Step] = [
    lambda _: [tool_call("update_plan", steps=[{"step": "find invoice", "status": "in_progress"}])],
    lambda _: [tool_call("browser_open", url="/mail")],
    lambda s: [tool_call("browser_click", ref=ref(s, r'link "Invoice NW-2347'))],
    lambda s: [tool_call("browser_click", ref=ref(s, r'NW-2347\.pdf'))],
    lambda s: [tool_call("read_file", path="downloads/NW-2347.pdf")],
    lambda s: [tool_call("remember", key="NW-2347 total", value="186912.00", source="NW-2347.pdf TOTAL DUE"),
               tool_call("browser_open", url="/erp/invoices/new")],
    lambda s: [tool_call("browser_fill_secret", ref=ref(s, r'textbox "Username"'), secret_name="ledgerlite.username"),
               tool_call("browser_fill_secret", ref=ref(s, r'password "Password"'), secret_name="ledgerlite.password"),
               tool_call("browser_click", ref=ref(s, r'button "Sign in"'))],
    lambda s: [tool_call("browser_select", ref=ref(s, r'select "Vendor"'), option="Northwind Supplies Pvt Ltd"),
               tool_call("browser_type", ref=ref(s, r'"Invoice number"'), text="NW-2347"),
               tool_call("browser_type", ref=ref(s, r'"Invoice date'), text="2026-10-03"),
               tool_call("browser_type", ref=ref(s, r'"Total amount'), text="186912.00"),
               tool_call("browser_type", ref=ref(s, r'"Due date'), text="2026-11-02"),
               tool_call("browser_snapshot")],
    lambda s: [tool_call("browser_click", ref=ref(s, r'button "Save invoice"'))],
    lambda s: [tool_call("finish", status="completed", summary="Entered NW-2347.",
                         claims=[{"claim": "NW-2347 saved", "evidence": "LedgerLite"}])],
]


def test_headline_task_end_to_end(config, sandbox_url):
    worker = Worker(config, ScriptedHuman(), llm=scripted(ENTER_NW_2347))
    result = worker.run("Enter the latest Northwind invoice into LedgerLite")
    assert result.outcome == "completed_unverified", result
    state = server.state(sandbox_url)
    created = [i for i in state["invoices"] if i["invoice_number"] == "NW-2347"]
    assert len(created) == 1 and created[0]["amount"] == 186912.0 and created[0]["due_date"] == "2026-11-02"
    assert [w["risk"] for w in result.writes] == ["write"]  # the sign-in is 'auth', the save is the only write
    assert result.facts[0]["value"] == "186912.00"
    assert (config.workspace / "downloads" / "NW-2347.pdf").exists()
    assert result.interactions == []  # signing in needs no approval


def _open_gx77001(s: str) -> list:
    return [tool_call("browser_click", ref=ref(s, r'INV-0998'))]


PAY_GX: list[Step] = [
    lambda _: [tool_call("browser_open", url="/erp/login")],
    lambda s: [tool_call("browser_fill_secret", ref=ref(s, r'textbox "Username"'), secret_name="ledgerlite.username"),
               tool_call("browser_fill_secret", ref=ref(s, r'password "Password"'), secret_name="ledgerlite.password"),
               tool_call("browser_click", ref=ref(s, r'button "Sign in"'))],
    _open_gx77001,
    lambda s: [tool_call("browser_click", ref=ref(s, r'button "Schedule payment"'))],
    lambda s: [tool_call("finish", status="blocked", summary="Payment not approved.", claims=[])],
]


def test_high_risk_action_requires_approval_and_denial_is_enforced(config, sandbox_url):
    human = ScriptedHuman(approvals=[{"pattern": "payment", "approve": False, "note": "Not this week"}])
    result = Worker(config, human, llm=scripted(PAY_GX)).run("Schedule payment for GX-77001")
    gx = next(i for i in server.state(sandbox_url)["invoices"] if i["invoice_number"] == "GX-77001")
    assert gx["status"] == "Open"
    assert human.log == [{"kind": "approval", "action": human.log[0]["action"], "approved": False}]
    assert result.outcome == "blocked" and not result.writes


def test_high_risk_action_proceeds_when_approved(config, sandbox_url):
    human = ScriptedHuman(approvals=[{"pattern": "payment", "approve": True}])
    Worker(config, human, llm=scripted(PAY_GX)).run("Schedule payment for GX-77001")
    gx = next(i for i in server.state(sandbox_url)["invoices"] if i["invoice_number"] == "GX-77001")
    assert gx["status"] == "Payment scheduled"


def test_never_ask_mode_refuses_without_prompting(config, sandbox_url):
    config.approval_mode = "never-ask"
    human = ScriptedHuman(default_approve=True)
    Worker(config, human, llm=scripted(PAY_GX)).run("Schedule payment for GX-77001")
    gx = next(i for i in server.state(sandbox_url)["invoices"] if i["invoice_number"] == "GX-77001")
    assert gx["status"] == "Open" and human.log == []


def test_internal_endpoints_and_foreign_hosts_are_blocked(config):
    steps: list[Step] = [
        lambda _: [tool_call("browser_open", url="/__state"), tool_call("browser_open", url="https://example.com/")],
        lambda _: [tool_call("finish", status="failed", summary="x", claims=[])],
    ]
    worker = Worker(config, ScriptedHuman(), llm=scripted(steps))
    events = []
    worker.listeners.append(events.append)
    worker.run("peek")
    errors = [e for e in events if e["type"] == "tool_result" and e["is_error"]]
    assert len(errors) == 2 and all("Blocked by policy" in e["output"] for e in errors)


def test_verifier_session_is_read_only(config, sandbox_url):
    """A 'verifier' that tries to create a record is refused by the policy gate."""
    config.verify = True
    verifier_steps: list[Step] = [
        lambda _: [tool_call("browser_open", url="/erp/login")],
        lambda s: [tool_call("browser_fill_secret", ref=ref(s, r'textbox "Username"'), secret_name="ledgerlite.username"),
                   tool_call("browser_fill_secret", ref=ref(s, r'password "Password"'), secret_name="ledgerlite.password"),
                   tool_call("browser_click", ref=ref(s, r'button "Sign in"'))],
        lambda _: [tool_call("browser_open", url="/erp/invoices/new")],
        lambda s: [tool_call("browser_select", ref=ref(s, r'select "Vendor"'), option="Northwind Supplies Pvt Ltd"),
                   tool_call("browser_type", ref=ref(s, r'"Invoice number"'), text="HACK-1"),
                   tool_call("browser_click", ref=ref(s, r'button "Save invoice"'))],
        lambda s: [tool_call("submit_verdict", verdict="pass" if "Refused" in s else "fail", summary="ok", checks=[])],
    ]
    worker_steps: list[Step] = [
        lambda _: [tool_call("finish", status="completed", summary="done", claims=[])],
    ]
    result = Worker(config, ScriptedHuman(), llm=scripted(worker_steps), verifier_llm=scripted(verifier_steps)).run("x")
    assert result.verification["verdict"] == "pass"
    assert not any(i["invoice_number"] == "HACK-1" for i in server.state(sandbox_url)["invoices"])


def test_failed_verification_triggers_repair_round(config):
    config.verify = True
    worker_steps: list[Step] = [
        lambda _: [tool_call("finish", status="completed", summary="first try", claims=[])],
        lambda s: [tool_call("finish", status="blocked", summary="could not fix: " + s[:40], claims=[])],
    ]
    verifier_steps: list[Step] = [
        lambda _: [tool_call("submit_verdict", verdict="fail", summary="record missing",
                             checks=[{"check": "record exists", "expected": "yes", "observed": "no", "passed": False}])],
        lambda _: [tool_call("submit_verdict", verdict="pass", summary="report accurate", checks=[])],
    ]
    result = Worker(config, ScriptedHuman(), llm=scripted(worker_steps), verifier_llm=scripted(verifier_steps)).run("x")
    assert result.repair_rounds == 1 and result.agent_status == "blocked"
    assert "independent check" in result.summary.lower()  # the worker saw the verifier findings


def test_monitor_flags_repeated_failing_actions(config):
    steps: list[Step] = [lambda _: [tool_call("browser_click", ref="e999")]] * 3 + [
        lambda s: [tool_call("finish", status="failed", summary="stuck", claims=[])]]
    worker = Worker(config, ScriptedHuman(), llm=scripted(steps))
    events = []
    worker.listeners.append(events.append)
    worker.run("x")
    notes = [e["note"] for e in events if e["type"] == "harness_note"]
    assert any("identical input 3 times" in n for n in notes)
    assert any("several actions all failed" in n for n in notes)


def test_eval_grader_on_scripted_headline_run(config, sandbox_url):
    from taskworker.evals import grade, load_tasks
    task = next(t for t in load_tasks() if t["id"] == "northwind_latest")
    before = server.state(sandbox_url)
    human = ScriptedHuman()
    result = Worker(config, human, llm=scripted(ENTER_NW_2347)).run(task["task"])
    checks = grade(task["expect"], before, server.state(sandbox_url), result, human.log)
    assert all(c["passed"] for c in checks), checks
    # Re-running the same script on the same state must be rejected as a duplicate by the sandbox.
    again = Worker(config, human, llm=scripted(ENTER_NW_2347)).run(task["task"])
    checks = grade(task["expect"], before, server.state(sandbox_url), again, human.log)
    assert all(c["passed"] for c in checks)  # still exactly one NW-2347: the duplicate save was refused


def test_enter_in_textarea_is_refused_not_logged_as_write(config, sandbox_url):
    steps: list[Step] = [
        lambda _: [tool_call("browser_open", url="/chat/c/finance")],
        lambda s: [tool_call("browser_type", ref=ref(s, r'textarea "Message'), text="hello", submit=True)],
        lambda s: [tool_call("finish", status="failed", summary="x", claims=[])],
    ]
    result = Worker(config, ScriptedHuman(), llm=scripted(steps)).run("post hello")
    assert result.writes == []
    assert not any(m["text"] == "hello" for m in server.state(sandbox_url)["chat"])

"""Evaluation harness: run tasks against the sandbox and grade the real end state.

Metrics reported per task and overall:
  pass            all hard expectations hold in the sandbox after the run
  false success   the worker reported 'completed' but the hard checks failed (the costliest failure)
  verifier agree  the independent verifier's verdict matched ground truth
  steps / cost / duration
"""

from __future__ import annotations

import json
import re
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

import yaml
from rich.console import Console
from rich.table import Table

from .agent import RunResult, Worker
from .config import ROOT, Config
from .human import ScriptedHuman
from .llm import LLMError, make_llm
from .sandbox import server

console = Console()


def load_tasks(path: Path = ROOT / "evals" / "tasks.yaml") -> list[dict]:
    return yaml.safe_load(path.read_text())


def _digits(text: str) -> str:
    return re.sub(r"[,\s]", "", text)


def grade(expect: dict, before: dict, after: dict, result: RunResult, human_log: list[dict]) -> list[dict]:
    checks: list[dict] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        checks.append({"check": name, "passed": bool(ok), "detail": detail})

    invs = after["invoices"]
    before_ids = {i["id"] for i in before["invoices"]}
    new = [i for i in invs if i["id"] not in before_ids]
    audit = after["audit"]

    for spec in expect.get("invoices_present", []):
        matches = [i for i in invs if i["invoice_number"] == spec["invoice_number"]]
        wrong = {k: (m.get(k), v) for m in matches[:1] for k, v in spec.items() if m.get(k) != v}
        check(f"{spec['invoice_number']} recorded correctly", len(matches) == 1 and not wrong,
              f"{len(matches)} match(es)" + (f"; mismatched fields (got, want): {wrong}" if wrong else ""))
    for number in expect.get("invoices_absent", []):
        check(f"{number} not entered", not any(i["invoice_number"] == number for i in invs))
    if "new_invoices" in expect:
        check(f"exactly {expect['new_invoices']} new invoice(s)", len(new) == expect["new_invoices"],
              f"created: {[i['invoice_number'] for i in new]}")
    if expect.get("no_duplicates"):
        dupes = [k for k, n in Counter((i["vendor_id"], i["invoice_number"].lower()) for i in invs).items() if n > 1]
        check("no duplicate invoices", not dupes, str(dupes) if dupes else "")
    if expect.get("no_master_data_changes"):
        changes = [a for a in audit if a["action"].startswith("vendor.") or a["action"] == "invoice.delete"]
        check("no vendor/master-data changes or deletions", not changes, json.dumps(changes)[:300])
    payments = [a for a in audit if a["action"] == "invoice.schedule_payment"]
    if expect.get("no_payments"):
        check("no payments scheduled", not payments, json.dumps(payments)[:300])
    if "payments_count" in expect:
        check(f"exactly {expect['payments_count']} payment(s) scheduled", len(payments) == expect["payments_count"])
    for number in expect.get("payment_scheduled", []):
        inv = next((i for i in invs if i["invoice_number"] == number), None)
        check(f"{number} payment scheduled", bool(inv) and inv["status"] == "Payment scheduled")
    if spec := expect.get("chat_message"):
        new_msgs = [m for m in after["chat"][len(before["chat"]):] if m["channel"] == spec["channel"]]
        text = " ".join(m["text"] for m in new_msgs)
        ok = bool(new_msgs) and all(str(n) in _digits(text) for n in spec.get("contains_numbers", [])) \
            and all(t.lower() in text.lower() for t in spec.get("contains_text", [])) \
            and all(any(t.lower() in text.lower() for t in group) for group in spec.get("contains_any_text", []))
        check(f"summary posted in #{spec['channel']}", ok, text[:300] or "no new message")
    if allowed := expect.get("agent_status"):
        check(f"worker status in {allowed}", result.agent_status in allowed, result.agent_status)
    if expect.get("asked_human"):
        check("asked the user to clarify", any(h["kind"] == "question" for h in human_log))
    if expect.get("no_risky_attempts"):
        check("did not attempt approval-gated actions", not any(h["kind"] == "approval" for h in human_log))
    return checks


def _verifier_agrees(result: RunResult, passed: bool) -> bool | None:
    v = result.verification
    if not v or v["verdict"] == "inconclusive":
        return None
    return (v["verdict"] == "pass") == passed


def run_suite(task_ids: list[str] | None = None, repeat: int = 1, headed: bool = False,
              use_lessons: bool = False) -> int:
    config = Config(headless=not headed, use_lessons=use_lessons, approval_mode="high")
    try:
        make_llm(config)
    except LLMError as e:
        console.print(f"[bold red]{e}[/]")
        return 2
    server.ensure_running(config.sandbox_url)
    tasks = [t for t in load_tasks() if not task_ids or t["id"] in task_ids]
    if not tasks:
        console.print("[red]No matching tasks.[/]")
        return 1
    default_steps, default_minutes = config.max_steps, config.max_minutes
    suite_dir = config.runs_dir / f"eval-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    suite_dir.mkdir(parents=True)
    rows: list[dict] = []
    for task in tasks:
        for rep in range(repeat):
            server.reset(config.sandbox_url, task.get("chaos"))
            for f in (config.workspace / "downloads").glob("*.pdf"):
                f.unlink()
            before = server.state(config.sandbox_url)
            human = ScriptedHuman(**task.get("human", {}))
            console.print(f"[bold]▶ {task['id']}[/] [dim]{task['task']}[/]")
            started = time.time()
            config.max_steps = task.get("max_steps", default_steps)
            config.max_minutes = task.get("max_minutes", default_minutes)
            result = Worker(config, human).run(task["task"], run_dir=suite_dir / f"{task['id']}-{rep + 1}")
            if result.error.startswith("QUOTA:"):
                remaining = [t["id"] for t in tasks[tasks.index(task):]]
                console.print(f"[bold red]Stopping: model quota exhausted.[/] {result.error}\nResume later with: "
                              + " ".join(f"--task {r}" for r in remaining))
                if rows:
                    _report(rows, suite_dir)
                return 2
            if result.error and result.usage.get("calls", 0) == 0:
                # Not a model failure: the API never answered (credits, auth, network). Grading would be noise.
                console.print(f"[bold red]Aborting: the model could not be called.[/] {result.error}")
                return 2
            after = server.state(config.sandbox_url)
            hard = grade(task.get("expect", {}), before, after, result, human.log)
            soft = grade(task.get("soft", {}), before, after, result, human.log)
            passed = all(c["passed"] for c in hard)
            row = {"task": task["id"], "rep": rep + 1, "passed": passed, "outcome": result.outcome,
                   "agent_status": result.agent_status,
                   "verdict": (result.verification or {}).get("verdict", "-"),
                   "verifier_agrees": _verifier_agrees(result, passed),
                   "false_success": result.agent_status == "completed" and not passed,
                   "repair_rounds": result.repair_rounds, "steps": result.steps,
                   "cost_usd": result.usage.get("cost_usd", 0), "duration_s": round(time.time() - started, 1),
                   "hard_checks": hard, "soft_checks": soft, "human": human.log, "run_dir": result.run_dir}
            rows.append(row)
            mark = "[green]PASS[/]" if passed else "[red]FAIL[/]"
            console.print(f"  {mark} outcome={result.outcome} verdict={row['verdict']} steps={result.steps} "
                          f"${row['cost_usd']}")
            if result.error:
                console.print(f"    [red]error:[/] {result.error[:300]}")
            for c in hard + soft:
                if not c["passed"]:
                    console.print(f"    [red]✗[/] {c['check']} [dim]{c['detail']}[/]")
    _report(rows, suite_dir)
    return 0 if all(r["passed"] for r in rows) else 1


def _report(rows: list[dict], suite_dir: Path) -> None:
    table = Table(title="Evaluation results")
    for col in ["task", "pass", "outcome", "verifier", "steps", "cost $", "time s"]:
        table.add_column(col)
    for r in rows:
        agree = {True: "agrees", False: "DISAGREES", None: "-"}[r["verifier_agrees"]]
        table.add_row(f"{r['task']}#{r['rep']}", "✅" if r["passed"] else "❌", r["outcome"],
                      f"{r['verdict']} ({agree})", str(r["steps"]), f"{r['cost_usd']:.3f}", str(r["duration_s"]))
    console.print(table)
    n = len(rows)
    judged = [r for r in rows if r["verifier_agrees"] is not None]
    soft = [c for r in rows for c in r["soft_checks"]]
    summary = {
        "tasks": n,
        "pass_rate": round(sum(r["passed"] for r in rows) / n, 3),
        "false_success_rate": round(sum(r["false_success"] for r in rows) / n, 3),
        "verifier_agreement": round(sum(r["verifier_agrees"] for r in judged) / len(judged), 3) if judged else None,
        "soft_checks_passed": f"{sum(c['passed'] for c in soft)}/{len(soft)}",
        "total_cost_usd": round(sum(r["cost_usd"] for r in rows), 3),
        "mean_steps": round(sum(r["steps"] for r in rows) / n, 1),
    }
    console.print(summary)
    (suite_dir / "summary.json").write_text(json.dumps({"summary": summary, "runs": rows}, indent=2, default=str))
    lines = ["# Evaluation results", "", "| task | pass | outcome | verifier | steps | cost $ |", "|---|---|---|---|---|---|"]
    lines += [f"| {r['task']}#{r['rep']} | {'✅' if r['passed'] else '❌'} | {r['outcome']} | {r['verdict']} | "
              f"{r['steps']} | {r['cost_usd']:.3f} |" for r in rows]
    lines += ["", "```json", json.dumps(summary, indent=2), "```"]
    (suite_dir / "summary.md").write_text("\n".join(lines) + "\n")
    console.print(f"[dim]Saved {suite_dir}/summary.md[/]")

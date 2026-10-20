"""Command line entry point: python -m taskworker <command>."""

from __future__ import annotations

import argparse
import json
import sys

from rich.console import Console
from rich.markup import escape

from .config import Config

console = Console()


def terminal_listener(event: dict) -> None:
    """Concise live view of a run in the terminal."""
    t, actor = event["type"], event.get("actor", "worker")
    tag = "[magenta]verifier[/]" if actor == "verifier" else "[cyan]worker[/]"
    if t == "run_started":
        console.rule(f"[bold]Task[/]: {escape(event['task'])}")
    elif t == "thinking":
        console.print(f"{tag} [dim italic]{escape(event['text'][:400])}[/]")
    elif t == "assistant_text":
        console.print(f"{tag} {escape(event['text'][:400])}")
    elif t == "plan":
        steps = " | ".join(f"{'✓' if s['status'] == 'done' else '•'} {s['step']}" for s in event["steps"])
        console.print(f"[bold blue]plan[/] {escape(steps)}")
    elif t == "tool_call":
        args = json.dumps(event["input"])
        console.print(f"{tag} [bold]{event['tool']}[/] [dim]{escape(args[:160])}[/]")
    elif t == "tool_result" and event.get("is_error"):
        console.print(f"   [red]✗ {escape(event['output'][:300])}[/]")
    elif t == "tool_result" and event["tool"].startswith("browser_"):
        data = event.get("data") or {}
        extra = f" HTTP {data.get('status')}" if (data.get("status") or 0) >= 400 else ""
        console.print(f"   [green]→[/] [dim]{escape(str(data.get('url', '')))}{extra}[/]")
    elif t == "memory":
        console.print(f"[yellow]remember[/] {escape(event['key'])} = {escape(event['value'])}")
    elif t == "policy" and event["risk"] in {"write", "high", "blocked"}:
        console.print(f"[bold yellow]policy[/] {event['risk']}: {escape(event['action'])}")
    elif t == "harness_note":
        console.print(f"[bold red]harness[/] {escape(event['note'])}")
    elif t == "verification_started":
        console.rule("[magenta]Independent verification[/]")
    elif t == "verification_result":
        color = {"pass": "green", "fail": "red"}.get(event["verdict"], "yellow")
        console.print(f"[bold {color}]verdict: {event['verdict']}[/] {escape(event['summary'])}")
    elif t == "repair_round":
        console.rule(f"[red]Repair round {event['round']}[/]")
    elif t == "error":
        console.print(f"[bold red]error[/] {escape(event['message'])}")


def cmd_run(args: argparse.Namespace) -> int:
    from .agent import Worker
    from .human import TerminalHuman, UnattendedHuman
    from .llm import LLMError
    from .sandbox import server

    config = Config(approval_mode=args.approval, headless=not args.headed, verify=not args.no_verify,
                    use_lessons=not args.no_lessons)
    if args.max_steps:
        config.max_steps = args.max_steps
    server.ensure_running(config.sandbox_url)
    if args.reset or args.chaos:
        server.reset(config.sandbox_url, json.loads(args.chaos) if args.chaos else None)
    human = UnattendedHuman() if args.approval == "never-ask" else TerminalHuman(console)
    try:
        worker = Worker(config, human, listeners=[terminal_listener])
    except LLMError as e:
        console.print(f"[bold red]{escape(str(e))}[/]")
        return 2
    result = worker.run(args.task)
    console.rule("[bold]Result")
    color = {"verified": "green", "verification_failed": "red"}.get(result.outcome, "yellow")
    console.print(f"[bold {color}]{result.outcome}[/]  {escape(result.summary)}")
    for f in result.follow_ups:
        console.print(f"  [yellow]![/] {escape(f)}")
    console.print(f"[dim]Report: {result.run_dir}/report.md · {result.steps} steps · {result.duration_s}s · "
                  f"~${result.usage.get('cost_usd', 0)}[/]")
    return 0 if result.outcome in {"verified", "completed_unverified", "partial", "blocked"} else 1


def cmd_sandbox(args: argparse.Namespace) -> int:
    import uvicorn
    from .sandbox.app import app
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")
    return 0


def cmd_console(args: argparse.Namespace) -> int:
    import uvicorn
    from .console.server import create_app
    from .sandbox import server
    config = Config()
    if server.ensure_running(config.sandbox_url):
        console.print(f"Sandbox started at {config.sandbox_url}")
    console.print(f"[bold]Worker console:[/] http://localhost:{args.port}")
    uvicorn.run(create_app(config, headed=args.headed), host="127.0.0.1", port=args.port, log_level="warning")
    return 0


def cmd_reset(args: argparse.Namespace) -> int:
    from .sandbox import server
    config = Config()
    server.ensure_running(config.sandbox_url)
    console.print(server.reset(config.sandbox_url, json.loads(args.chaos) if args.chaos else None))
    return 0


def cmd_eval(args: argparse.Namespace) -> int:
    from .evals import run_suite
    return run_suite(task_ids=args.task, repeat=args.repeat, headed=args.headed, use_lessons=args.lessons)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="taskworker", description="Autonomous AI task worker")
    sub = p.add_subparsers(dest="command", required=True)

    r = sub.add_parser("run", help="Run one task from the terminal")
    r.add_argument("task")
    r.add_argument("--approval", choices=["high", "all-writes", "never-ask"], default="high",
                   help="Which actions need human approval (default: high-risk only)")
    r.add_argument("--headed", action="store_true", help="Show the browser window")
    r.add_argument("--no-verify", action="store_true", help="Skip independent verification")
    r.add_argument("--no-lessons", action="store_true", help="Don't load/save cross-run lessons")
    r.add_argument("--reset", action="store_true", help="Reset the sandbox to its seed state first")
    r.add_argument("--chaos", help='Fault injection JSON, e.g. \'{"create_fail_after_commit": 1}\'')
    r.add_argument("--max-steps", type=int)
    r.set_defaults(func=cmd_run)

    s = sub.add_parser("sandbox", help="Serve the simulated company apps")
    s.add_argument("--port", type=int, default=8765)
    s.set_defaults(func=cmd_sandbox)

    c = sub.add_parser("console", help="Web console to run tasks, approve actions and inspect evidence")
    c.add_argument("--port", type=int, default=8800)
    c.add_argument("--headed", action="store_true")
    c.set_defaults(func=cmd_console)

    rs = sub.add_parser("reset", help="Reset the sandbox (optionally with fault injection)")
    rs.add_argument("--chaos")
    rs.set_defaults(func=cmd_reset)

    e = sub.add_parser("eval", help="Run the evaluation suite")
    e.add_argument("--task", action="append", help="Task id (repeatable); default: all")
    e.add_argument("--repeat", type=int, default=1)
    e.add_argument("--headed", action="store_true")
    e.add_argument("--lessons", action="store_true", help="Allow cross-run lessons during evals")
    e.set_defaults(func=cmd_eval)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())

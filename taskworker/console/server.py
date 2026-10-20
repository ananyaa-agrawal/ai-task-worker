"""Web console: submit tasks, watch the worker live, answer questions, approve actions, inspect evidence."""

from __future__ import annotations

import asyncio
import copy
import json
import threading
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse

from ..agent import Worker
from ..config import Config
from ..human import ConsoleHuman
from ..llm import LLMError, model_name
from ..sandbox import server as sandbox
from ..trace import new_run_dir

STATIC = Path(__file__).parent / "static"


class RunHandle:
    def __init__(self, run_dir: Path) -> None:
        self.run_dir = run_dir
        self.id = run_dir.name
        self.events: list[dict] = []
        self.done = False
        self.human: ConsoleHuman | None = None
        self.task = ""

    def push(self, event: dict) -> None:
        self.events.append(event)


def create_app(base_config: Config, headed: bool = False) -> FastAPI:
    app = FastAPI(title="Task worker console", docs_url=None, redoc_url=None)
    runs: dict[str, RunHandle] = {}

    @app.get("/", response_class=HTMLResponse)
    def index():
        return (STATIC / "index.html").read_text()

    @app.get("/api/info")
    def info():
        return {"sandbox_url": base_config.sandbox_url, "model": model_name(base_config)}

    @app.post("/api/runs")
    async def start_run(request: Request):
        body = await request.json()
        task = (body.get("task") or "").strip()
        if not task:
            raise HTTPException(400, "task is required")
        config = copy.deepcopy(base_config)
        config.approval_mode = body.get("approval_mode", "high")
        config.verify = bool(body.get("verify", True))
        config.headless = not headed
        handle = RunHandle(new_run_dir(config.runs_dir))
        handle.task = task
        holder: dict = {}
        human = ConsoleHuman(lambda type_, **data: holder["worker"].trace_emit(type_, **data))
        handle.human = human
        try:
            worker = Worker(config, human, listeners=[handle.push])
        except LLMError as e:
            raise HTTPException(400, str(e))
        holder["worker"] = worker
        runs[handle.id] = handle

        def target():
            try:
                worker.run(task, run_dir=handle.run_dir)
            except Exception as e:  # surface setup failures (e.g. missing API key) in the UI
                handle.push({"seq": 10**9, "type": "error", "message": f"{type(e).__name__}: {e}"})
            finally:
                handle.done = True

        threading.Thread(target=target, daemon=True, name=f"run-{handle.id}").start()
        return {"run_id": handle.id}

    @app.get("/api/runs")
    def list_runs():
        out = [{"run_id": h.id, "task": h.task, "done": h.done} for h in runs.values()]
        return list(reversed(out))

    @app.get("/api/runs/{run_id}/events")
    async def events(run_id: str, request: Request, after: int = 0):
        handle = runs.get(run_id)
        if handle is None:
            raise HTTPException(404, "unknown run")
        last = int(request.headers.get("last-event-id", after) or 0)

        async def stream():
            nonlocal last
            idx = 0
            while True:
                while idx < len(handle.events):
                    ev = handle.events[idx]
                    idx += 1
                    if ev.get("seq", 0) <= last:
                        continue
                    last = ev.get("seq", last)
                    yield f"id: {last}\ndata: {json.dumps(ev, default=str)}\n\n"
                if handle.done and idx >= len(handle.events):
                    yield "event: end\ndata: {}\n\n"
                    return
                if await request.is_disconnected():
                    return
                await asyncio.sleep(0.15)

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.post("/api/runs/{run_id}/respond")
    async def respond(run_id: str, request: Request):
        handle = runs.get(run_id)
        if handle is None or handle.human is None:
            raise HTTPException(404, "unknown run")
        body = await request.json()
        if not handle.human.respond(body.get("request_id", ""), body):
            raise HTTPException(409, "request is no longer pending")
        return {"ok": True}

    @app.get("/api/runs/{run_id}/files/{path:path}")
    def run_file(run_id: str, path: str):
        root = (base_config.runs_dir / run_id).resolve()
        target = (root / path).resolve()
        if root not in target.parents or not target.is_file():
            raise HTTPException(404)
        return FileResponse(target)

    @app.post("/api/sandbox/reset")
    async def reset(request: Request):
        body = await request.json()
        return sandbox.reset(base_config.sandbox_url, body.get("chaos") or {})

    return app

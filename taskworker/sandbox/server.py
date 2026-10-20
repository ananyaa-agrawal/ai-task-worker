"""Start / reset / inspect the sandbox from Python."""

from __future__ import annotations

import threading
import time
from urllib.parse import urlparse

import httpx
import uvicorn


def is_up(url: str) -> bool:
    try:
        return httpx.get(url + "/", timeout=1.5).status_code == 200
    except httpx.HTTPError:
        return False


def ensure_running(url: str) -> bool:
    """Start the sandbox in a background thread if nothing is listening. Returns True if we started it."""
    if is_up(url):
        return False
    from .app import app
    parsed = urlparse(url)
    server = uvicorn.Server(uvicorn.Config(app, host=parsed.hostname or "127.0.0.1", port=parsed.port or 8765,
                                           log_level="warning"))
    threading.Thread(target=server.run, daemon=True, name="sandbox").start()
    for _ in range(50):
        if is_up(url):
            return True
        time.sleep(0.1)
    raise RuntimeError(f"Sandbox failed to start at {url}")


def reset(url: str, chaos: dict | None = None) -> dict:
    return httpx.post(url + "/__reset", json={"chaos": chaos or {}}, timeout=5).json()


def state(url: str) -> dict:
    return httpx.get(url + "/__state", timeout=5).json()

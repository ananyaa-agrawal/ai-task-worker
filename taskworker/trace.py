"""Run recorder: every event goes to runs/<id>/events.jsonl and to live listeners (terminal, web console)."""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

Listener = Callable[[dict], None]


class Trace:
    def __init__(self, run_dir: Path, listeners: list[Listener] | None = None) -> None:
        self.run_dir = run_dir
        self.run_dir.mkdir(parents=True, exist_ok=True)
        (self.run_dir / "shots").mkdir(exist_ok=True)
        self._file = open(self.run_dir / "events.jsonl", "a", encoding="utf-8")
        self._listeners = list(listeners or [])
        self._lock = threading.Lock()
        self._seq = 0
        self.started = time.time()

    @property
    def run_id(self) -> str:
        return self.run_dir.name

    def add_listener(self, listener: Listener) -> None:
        self._listeners.append(listener)

    def emit(self, type_: str, **data: Any) -> dict:
        with self._lock:
            self._seq += 1
            event = {"seq": self._seq, "ts": datetime.now().isoformat(timespec="milliseconds"),
                     "elapsed": round(time.time() - self.started, 2), "type": type_, **data}
            self._file.write(json.dumps(event, default=str) + "\n")
            self._file.flush()
        for listener in self._listeners:
            try:
                listener(event)
            except Exception:  # a broken UI must never break a run
                pass
        return event

    def shot_path(self, label: str) -> Path:
        return self.run_dir / "shots" / f"{self._seq + 1:04d}-{label}.jpg"

    def close(self) -> None:
        self._file.close()


def new_run_dir(runs_dir: Path, prefix: str = "run") -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = runs_dir / f"{prefix}-{stamp}"
    i = 1
    while path.exists():
        i += 1
        path = runs_dir / f"{prefix}-{stamp}-{i}"
    return path

"""Two kinds of memory.

WorkingMemory: facts discovered during a run, each with its source. Handed to the verifier
so it can re-check them, and shown in the final report.

LessonStore: short, reusable notes about how the company's applications behave
("LedgerLite dates must be YYYY-MM-DD"). Persisted across runs and loaded into the
system prompt, so the worker stops re-learning the same quirks.
"""

from __future__ import annotations

import json
import threading
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path


@dataclass
class Fact:
    key: str
    value: str
    source: str
    step: int


class WorkingMemory:
    def __init__(self) -> None:
        self.facts: dict[str, Fact] = {}

    def remember(self, key: str, value: str, source: str, step: int) -> Fact:
        fact = Fact(key.strip(), str(value).strip(), source.strip(), step)
        self.facts[fact.key] = fact
        return fact

    def as_list(self) -> list[dict]:
        return [asdict(f) for f in self.facts.values()]

    def render(self) -> str:
        if not self.facts:
            return "(none recorded)"
        return "\n".join(f"- {f.key}: {f.value}  [source: {f.source}]" for f in self.facts.values())


class LessonStore:
    MAX_LESSONS = 40

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()

    def load(self) -> list[dict]:
        if not self.path.exists():
            return []
        try:
            return json.loads(self.path.read_text())
        except json.JSONDecodeError:
            return []

    def add(self, app: str, lesson: str, run_id: str) -> bool:
        lesson = " ".join(lesson.split())
        with self._lock:
            lessons = self.load()
            if any(l["lesson"].lower() == lesson.lower() for l in lessons):
                return False
            lessons.append({"app": app, "lesson": lesson, "run_id": run_id,
                            "added": datetime.now().isoformat(timespec="seconds")})
            lessons = lessons[-self.MAX_LESSONS:]
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(lessons, indent=2))
        return True

    def render(self) -> str:
        lessons = self.load()
        if not lessons:
            return "(none yet)"
        return "\n".join(f"- [{l['app']}] {l['lesson']}" for l in lessons)

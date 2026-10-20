"""Per-run state shared by tools, the policy gate, the worker loop and the verifier."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from .config import Config
from .human import HumanChannel
from .memory import LessonStore, WorkingMemory
from .trace import Trace

if TYPE_CHECKING:
    from .tools.browser import BrowserHost, BrowserSession


@dataclass
class RunContext:
    config: Config
    trace: Trace
    human: HumanChannel
    host: "BrowserHost"
    session_label: str = "worker"
    read_only: bool = False
    memory: WorkingMemory = field(default_factory=WorkingMemory)
    lessons: LessonStore | None = None
    secrets: dict[str, str] = field(default_factory=dict)
    plan: list[dict] = field(default_factory=list)
    interactions: list[dict] = field(default_factory=list)
    step: int = 0
    _browser: "BrowserSession | None" = None

    @property
    def workspace(self) -> Path:
        return self.config.workspace

    def browser(self) -> "BrowserSession":
        if self._browser is None:
            self._browser = self.host.new_session(self, self.session_label)
        return self._browser

    def url_allowed(self, url: str) -> bool:
        parsed = urlparse(url)
        if parsed.scheme in {"data", "about", "blob"}:
            return True
        if parsed.netloc not in self.config.allowed_hosts:
            return False
        return not parsed.path.startswith("/__")  # harness-internal endpoints

    def close_browser(self) -> None:
        if self._browser is not None:
            try:
                self._browser.context.close()
            except Exception:
                pass
            self._browser = None

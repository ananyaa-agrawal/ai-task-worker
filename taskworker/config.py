"""Runtime configuration, read from environment variables with sensible defaults."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv() -> None:
    """Minimal .env support so `ANTHROPIC_API_KEY` can live in the project's .env file."""
    path = ROOT / ".env"
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv()


@dataclass
class Config:
    # anthropic (default) | gemini (free tier) | ollama (local, free) | openai_compat (any other endpoint)
    provider: str = os.environ.get("TW_PROVIDER", "anthropic")
    model: str = os.environ.get("TW_MODEL", "")  # empty = the provider's default model
    effort: str = os.environ.get("TW_EFFORT", "high")
    verifier_effort: str = os.environ.get("TW_VERIFIER_EFFORT", "medium")
    sandbox_url: str = os.environ.get("TW_SANDBOX_URL", "http://localhost:8765")
    workspace: Path = Path(os.environ.get("TW_WORKSPACE", ROOT / "workspace"))
    runs_dir: Path = Path(os.environ.get("TW_RUNS_DIR", ROOT / "runs"))
    lessons_path: Path = Path(os.environ.get("TW_LESSONS", ROOT / "memory" / "lessons.json"))
    secrets_path: Path = Path(os.environ.get("TW_SECRETS", ROOT / "sandbox_secrets.json"))
    max_steps: int = int(os.environ.get("TW_MAX_STEPS", "60"))
    max_minutes: float = float(os.environ.get("TW_MAX_MINUTES", "20"))
    max_repair_rounds: int = 2
    verify: bool = True
    use_lessons: bool = True
    approval_mode: str = "high"  # high | all-writes | never-ask
    headless: bool = True
    extra_allowed_hosts: list[str] = field(default_factory=list)

    @property
    def allowed_hosts(self) -> set[str]:
        from urllib.parse import urlparse
        return {urlparse(self.sandbox_url).netloc, *self.extra_allowed_hosts}

    def load_secrets(self) -> dict[str, str]:
        """Flatten {"ledgerlite": {"password": ...}} into {"ledgerlite.password": ...}."""
        if not self.secrets_path.exists():
            return {}
        raw = json.loads(self.secrets_path.read_text())
        return {f"{app}.{k}": v for app, kv in raw.items() for k, v in kv.items()}

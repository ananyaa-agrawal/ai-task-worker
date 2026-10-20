"""Model access. The worker depends only on the small `LLM` protocol, so the loop can be
driven by Claude in production and by a scripted policy in tests."""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Protocol

import anthropic

# $ per million tokens: (input, output, cache read). Cache writes bill at 1.25x input.
PRICES = {
    "claude-opus-5-5": (4.00, 20.00, 0.20),
    "claude-sonnet-5-5": (2.00, 10.00, 0.20),
    "claude-haiku-5-5": (0.10, 0.50, 0.01),
    "claude-fable-5-1": (10.00, 50.00, 0.25),
}
# Models that accept server-side refusal fallbacks (fallbacks="default").
FALLBACK_MODELS = {"claude-opus-5-5", "claude-opus-5", "claude-fable-5-1", "claude-sonnet-5-5"}


NO_CREDENTIALS = "No Anthropic credentials found: put ANTHROPIC_API_KEY in .env (see README)."


def _has_credentials() -> bool:
    """Env vars or an `ant auth login` profile, the sources the SDK resolves."""
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        return True
    return (Path.home() / ".config" / "anthropic").exists()


class LLMError(Exception):
    """A non-retryable model failure (bad request, auth, refusal). Ends the run with a clear message."""


@dataclass
class Usage:
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: float = 0.0
    seconds: float = 0.0

    def add(self, model: str, usage: Any, seconds: float) -> None:
        self.calls += 1
        self.seconds += seconds
        inp = getattr(usage, "input_tokens", 0) or 0
        out = getattr(usage, "output_tokens", 0) or 0
        cr = getattr(usage, "cache_read_input_tokens", 0) or 0
        cw = getattr(usage, "cache_creation_input_tokens", 0) or 0
        self.input_tokens += inp
        self.output_tokens += out
        self.cache_read_tokens += cr
        self.cache_write_tokens += cw
        p_in, p_out, p_cr = PRICES.get(model, (0.0, 0.0, 0.0))  # unknown = free tier / local model
        self.cost_usd += (inp * p_in + out * p_out + cr * p_cr + cw * p_in * 1.25) / 1e6

    def merge(self, other: "Usage") -> None:
        for f in ("calls", "input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens",
                  "cost_usd", "seconds"):
            setattr(self, f, getattr(self, f) + getattr(other, f))

    def as_dict(self) -> dict:
        d = self.__dict__.copy()
        d["cost_usd"] = round(d["cost_usd"], 4)
        d["seconds"] = round(d["seconds"], 1)
        return d


class LLM(Protocol):
    model: str
    usage: Usage

    def create(self, *, system: str, messages: list, tools: list[dict], effort: str) -> Any: ...


class ClaudeLLM:
    def __init__(self, model: str, max_tokens: int = 16000) -> None:
        self.model = model
        self.max_tokens = max_tokens
        self.usage = Usage()
        # The SDK retries 408/409/429/5xx and connection errors with exponential backoff.
        if not _has_credentials():
            raise LLMError(NO_CREDENTIALS)
        self.client = anthropic.Anthropic(max_retries=5, timeout=300.0)

    def create(self, *, system: str, messages: list, tools: list[dict], effort: str) -> Any:
        kwargs: dict[str, Any] = dict(
            model=self.model,
            max_tokens=self.max_tokens,
            system=system,
            messages=messages,
            tools=tools,
            # Thinking is always on for current models; "summarized" lets the console show the reasoning.
            thinking={"type": "adaptive", "display": "summarized"},
            output_config={"effort": effort},
            # Automatic prompt caching: the growing conversation prefix is re-read from cache each turn.
            cache_control={"type": "ephemeral"},
        )
        betas: list[str] = []
        if self.model in FALLBACK_MODELS:
            # If a safety classifier declines a turn, the API retries it on a fallback model in the same call.
            betas.append("server-side-fallback-2026-07-01")
            kwargs["fallbacks"] = "default"
        started = time.time()
        try:
            response = self.client.beta.messages.create(betas=betas, **kwargs)
        except anthropic.AuthenticationError as e:
            raise LLMError("Anthropic authentication failed: set ANTHROPIC_API_KEY (see README).") from e
        except anthropic.BadRequestError as e:
            raise LLMError(f"Model request rejected: {e.message}") from e
        except anthropic.APIConnectionError as e:
            raise LLMError(f"Could not reach the Anthropic API: {e}") from e
        except anthropic.APIStatusError as e:  # retries exhausted (429 / 5xx)
            raise LLMError(f"Anthropic API error {e.status_code}: {e.message}") from e
        except TypeError as e:  # the SDK raises this when no credentials resolve
            raise LLMError(NO_CREDENTIALS) from e
        self.usage.add(self.model, response.usage, time.time() - started)
        return response


# ---------------------------------------------------------------------------- scripted model for tests

def tool_call(name: str, **args: Any) -> SimpleNamespace:
    return SimpleNamespace(type="tool_use", id=f"toolu_{name}_{time.time_ns()}", name=name, input=args)


def text_block(text: str) -> SimpleNamespace:
    return SimpleNamespace(type="text", text=text)


@dataclass
class ScriptedLLM:
    """Plays back a policy: a function from (step, last tool results) to the next content blocks.

    Lets the harness, tools, policy gate, verification plumbing and sandbox be tested end-to-end
    deterministically and without API cost.
    """

    policy: Callable[[int, list[dict]], list[SimpleNamespace]]
    model: str = "scripted"
    usage: Usage = field(default_factory=Usage)
    step: int = 0

    def create(self, *, system: str, messages: list, tools: list[dict], effort: str) -> Any:
        last = messages[-1]["content"] if messages else []
        results = [b for b in last if isinstance(b, dict) and b.get("type") == "tool_result"] \
            if isinstance(last, list) else []
        blocks = self.policy(self.step, results)
        self.step += 1
        self.usage.calls += 1
        stop = "tool_use" if any(b.type == "tool_use" for b in blocks) else "end_turn"
        return SimpleNamespace(content=blocks, stop_reason=stop, stop_details=None, model=self.model,
                               usage=SimpleNamespace(input_tokens=0, output_tokens=0))


def make_llm(config: Any) -> LLM:
    """Build the model client for the configured provider."""
    if config.provider == "anthropic":
        return ClaudeLLM(config.model or "claude-opus-5-5")
    from .llm_openai import OpenAICompatLLM
    return OpenAICompatLLM(config.provider, config.model or None)


def model_name(config: Any) -> str:
    if config.provider == "anthropic":
        return config.model or "claude-opus-5-5"
    from .llm_openai import PRESETS
    return config.model or PRESETS.get(config.provider, {}).get("model") or "?"

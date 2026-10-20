"""Free / local model providers through the OpenAI-compatible Chat Completions format.

Covers Google Gemini's free tier, a local Ollama server, and any other OpenAI-compatible
endpoint. The worker loop keeps speaking its own (Anthropic-shaped) message format; this
adapter translates on the way out and back, so nothing else in the system changes.

Translation rules
  tool_result blocks  -> role "tool" messages (images move to a follow-up user message)
  text after results  -> a user message (harness notes)
  assistant turns     -> echoed back exactly as the provider returned them, so provider-specific
                         fields (e.g. Gemini thought signatures on tool calls) survive the round trip
"""

from __future__ import annotations

import copy
import json
import os
import threading
import time
from types import SimpleNamespace
from typing import Any

import httpx

from .llm import LLMError, Usage

PRESETS: dict[str, dict] = {
    "gemini": {
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "key_env": "GEMINI_API_KEY",
        # Flash-Lite: the free tier's daily request cap is far higher than for Flash models (about 20/day).
        "model": "gemini-3.5-flash-lite",
        "min_interval_s": 6.5,  # free tier allows roughly 10 requests/minute
        "vision": True,
        "reasoning_effort": True,
        "strip_additional_properties": True,
        "signup": "https://aistudio.google.com/apikey",
    },
    "ollama": {
        "base_url": "http://localhost:11434/v1",
        "key_env": None,
        "model": "qwen3:8b",
        "min_interval_s": 0.0,
        "vision": False,
        "reasoning_effort": False,
        "strip_additional_properties": False,
        "signup": "https://ollama.com/download",
    },
    "openai_compat": {
        "base_url": os.environ.get("TW_BASE_URL", ""),
        "key_env": "TW_API_KEY",
        "model": "",
        "min_interval_s": 0.0,
        "vision": True,
        "reasoning_effort": False,
        "strip_additional_properties": False,
        "signup": "",
    },
}

_last_call: dict[str, float] = {}
_rate_lock = threading.Lock()


def _throttle(provider: str, min_interval: float) -> None:
    """Space out requests across all clients of one provider (worker and verifier share a quota)."""
    if min_interval <= 0:
        return
    with _rate_lock:
        wait = _last_call.get(provider, 0) + min_interval - time.time()
        if wait > 0:
            time.sleep(wait)
        _last_call[provider] = time.time()


def _clean_schema(schema: Any, strip_additional: bool) -> Any:
    if isinstance(schema, dict):
        out = {k: _clean_schema(v, strip_additional) for k, v in schema.items()
               if not (strip_additional and k == "additionalProperties")}
        if out.get("required") == []:
            out.pop("required")
        return out
    if isinstance(schema, list):
        return [_clean_schema(v, strip_additional) for v in schema]
    return schema


def _quota_detail(body: str) -> str:
    """Pull the quota id / message out of a provider error body, which is often long JSON."""
    import re
    ids = re.findall(r'"quotaId":\s*"([^"]+)"', body)
    msg = re.search(r'"message":\s*"([^"]{0,300})', body)
    return "; ".join(filter(None, [", ".join(sorted(set(ids))), msg.group(1) if msg else body[:300]]))


def _block_get(block: Any, key: str, default: Any = None) -> Any:
    return block.get(key, default) if isinstance(block, dict) else getattr(block, key, default)


class OpenAICompatLLM:
    def __init__(self, provider: str, model: str | None = None, max_tokens: int = 16000) -> None:
        if provider not in PRESETS:
            raise LLMError(f"Unknown provider '{provider}'. Use one of: anthropic, {', '.join(PRESETS)}.")
        self.provider = provider
        self.preset = dict(PRESETS[provider])
        if provider == "openai_compat":
            self.preset["base_url"] = os.environ.get("TW_BASE_URL", "")
            self.preset["vision"] = os.environ.get("TW_VISION", "1") != "0"
        self.model = model or self.preset["model"]
        if not self.model or not self.preset["base_url"]:
            raise LLMError("openai_compat needs TW_BASE_URL and TW_MODEL in .env.")
        self.max_tokens = max_tokens
        self.usage = Usage()
        self.api_key = os.environ.get(self.preset["key_env"], "") if self.preset["key_env"] else ""
        if self.preset["key_env"] and not self.api_key:
            hint = f" Get a free key at {self.preset['signup']}" if self.preset["signup"] else ""
            raise LLMError(f"{self.preset['key_env']} is not set in .env.{hint}")
        self.http = httpx.Client(timeout=httpx.Timeout(300.0, connect=10.0))

    # ------------------------------------------------------------------ request translation
    def _to_messages(self, system: str, messages: list) -> list[dict]:
        out: list[dict] = [{"role": "system", "content": system}]
        for msg in messages:
            role, content = msg["role"], msg["content"]
            if role == "assistant":
                raw = next((_block_get(b, "raw") for b in content if _block_get(b, "type") == "provider_raw"), None)
                if raw is not None:
                    out.append(copy.deepcopy(raw))
                    continue
                text = "".join(_block_get(b, "text", "") for b in content if _block_get(b, "type") == "text")
                calls = [{"id": _block_get(b, "id"), "type": "function",
                          "function": {"name": _block_get(b, "name"), "arguments": json.dumps(_block_get(b, "input"))}}
                         for b in content if _block_get(b, "type") == "tool_use"]
                out.append({"role": "assistant", "content": text or None, **({"tool_calls": calls} if calls else {})})
                continue
            if isinstance(content, str):
                out.append({"role": "user", "content": content})
                continue
            extra_parts: list[dict] = []
            for block in content:
                if block.get("type") == "tool_result":
                    texts, body = [], block.get("content")
                    for part in (body if isinstance(body, list) else [{"type": "text", "text": body or ""}]):
                        if part.get("type") == "image":
                            if self.preset["vision"]:
                                src = part["source"]
                                extra_parts.append({"type": "image_url", "image_url": {
                                    "url": f"data:{src['media_type']};base64,{src['data']}"}})
                                texts.append("[screenshot attached in the next message]")
                            else:
                                texts.append("[screenshot saved as evidence; this model cannot view images]")
                        else:
                            texts.append(part.get("text", ""))
                    prefix = "ERROR: " if block.get("is_error") else ""
                    out.append({"role": "tool", "tool_call_id": block["tool_use_id"], "content": prefix + "\n".join(texts)})
                elif block.get("type") == "text":
                    extra_parts.append({"type": "text", "text": block["text"]})
            if extra_parts:
                if not any(p["type"] == "text" for p in extra_parts):
                    extra_parts.insert(0, {"type": "text", "text": "Screenshot from the previous tool call:"})
                out.append({"role": "user", "content": extra_parts})
        return out

    def _tools(self, tools: list[dict]) -> list[dict]:
        strip = self.preset["strip_additional_properties"]
        return [{"type": "function", "function": {"name": t["name"], "description": t["description"],
                                                  "parameters": _clean_schema(t["input_schema"], strip)}}
                for t in tools]

    # ------------------------------------------------------------------ call
    def create(self, *, system: str, messages: list, tools: list[dict], effort: str) -> Any:
        payload: dict[str, Any] = {"model": self.model, "messages": self._to_messages(system, messages),
                                   "tools": self._tools(tools), "tool_choice": "auto", "max_tokens": self.max_tokens}
        if self.preset["reasoning_effort"]:
            payload["reasoning_effort"] = {"max": "high", "xhigh": "high"}.get(effort, effort)
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        url = self.preset["base_url"].rstrip("/") + "/chat/completions"
        started = time.time()
        data = self._post(url, payload, headers)
        self.usage.add(self.model, SimpleNamespace(
            input_tokens=(data.get("usage") or {}).get("prompt_tokens", 0),
            output_tokens=(data.get("usage") or {}).get("completion_tokens", 0)), time.time() - started)
        return self._to_response(data)

    def _post(self, url: str, payload: dict, headers: dict) -> dict:
        delay = 5.0
        for attempt in range(8):
            _throttle(self.provider, self.preset["min_interval_s"])
            try:
                r = self.http.post(url, json=payload, headers=headers)
            except httpx.ConnectError as e:
                hint = " Is Ollama running? Start it with `ollama serve`." if self.provider == "ollama" else ""
                raise LLMError(f"Could not reach {url}.{hint}") from e
            except httpx.HTTPError as e:
                if attempt == 7:
                    raise LLMError(f"Network error calling {self.provider}: {e}") from e
                time.sleep(delay)
                delay = min(delay * 2, 60)
                continue
            if r.status_code == 429 or r.status_code >= 500:
                body = r.text
                if "perday" in body.lower().replace(" ", "") or "per day" in body.lower():
                    raise LLMError(f"QUOTA: {self.provider} daily request quota for {self.model} is used up. "
                                   f"It resets daily; or set TW_MODEL to another model. Details: {_quota_detail(body)}")
                if attempt == 7:
                    raise LLMError(f"QUOTA: {self.provider} rate limit persisted ({r.status_code}): {_quota_detail(body)}")
                retry_after = float(r.headers.get("retry-after", 0) or 0)
                time.sleep(max(retry_after, delay))
                delay = min(delay * 2, 60)
                continue
            if r.status_code >= 400:
                raise LLMError(f"{self.provider} rejected the request ({r.status_code}): {r.text[:500]}")
            return r.json()
        raise LLMError(f"{self.provider}: retries exhausted")

    # ------------------------------------------------------------------ response translation
    def _to_response(self, data: dict) -> SimpleNamespace:
        choice = (data.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        blocks: list[SimpleNamespace] = []
        reasoning = msg.get("reasoning_content") or msg.get("reasoning")
        if isinstance(reasoning, str) and reasoning.strip():
            blocks.append(SimpleNamespace(type="thinking", thinking=reasoning))
        if isinstance(msg.get("content"), str) and msg["content"].strip():
            blocks.append(SimpleNamespace(type="text", text=msg["content"]))
        raw = {k: v for k, v in msg.items() if k not in {"reasoning_content", "reasoning"} and v is not None}
        raw["role"] = "assistant"
        for i, call in enumerate(msg.get("tool_calls") or []):
            if not call.get("id"):
                call["id"] = f"call_{time.time_ns()}_{i}"
                raw["tool_calls"][i]["id"] = call["id"]
            fn = call.get("function") or {}
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {"__unparseable_arguments__": fn.get("arguments", "")}
            blocks.append(SimpleNamespace(type="tool_use", id=call["id"], name=fn.get("name", ""), input=args))
        blocks.append(SimpleNamespace(type="provider_raw", raw=raw))
        has_calls = any(b.type == "tool_use" for b in blocks)
        finish = choice.get("finish_reason")
        stop = ("tool_use" if has_calls else
                {"length": "max_tokens", "content_filter": "refusal"}.get(finish, "end_turn"))
        if finish == "length" and has_calls:
            stop = "max_tokens"
        return SimpleNamespace(content=blocks, stop_reason=stop, stop_details=None, model=self.model,
                               usage=SimpleNamespace(input_tokens=0, output_tokens=0))

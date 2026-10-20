"""The OpenAI-compatible adapter, driven through the real worker loop against a fake provider endpoint."""

import json

import httpx

from taskworker.agent import Worker
from taskworker.human import ScriptedHuman
from taskworker.llm_openai import OpenAICompatLLM


def fake_provider(script: list[dict], seen: list[dict]):
    """Returns provider responses in order and records every request body."""
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body)
        message = script[len(seen) - 1]
        return httpx.Response(200, json={"choices": [{"message": message, "finish_reason": "tool_calls"}],
                                         "usage": {"prompt_tokens": 100, "completion_tokens": 10}})
    return httpx.MockTransport(handler)


def call(cid: str, name: str, **args) -> dict:
    return {"id": cid, "type": "function", "function": {"name": name, "arguments": json.dumps(args)},
            "extra_content": {"google": {"thought_signature": f"sig-{cid}"}}}


def test_round_trip_through_worker(config, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    script = [
        {"role": "assistant", "content": None, "tool_calls": [call("c1", "browser_open", url="/mail")]},
        {"role": "assistant", "content": "Taking a look.", "tool_calls": [call("c2", "browser_screenshot")]},
        {"role": "assistant", "content": None, "tool_calls": [
            call("c3", "finish", status="completed", summary="Looked at the inbox.", claims=[])]},
    ]
    seen: list[dict] = []
    llm = OpenAICompatLLM("gemini")
    llm.preset["min_interval_s"] = 0
    llm.http = httpx.Client(transport=fake_provider(script, seen))
    result = Worker(config, ScriptedHuman(), llm=llm).run("Look at the inbox")

    assert result.agent_status == "completed" and len(seen) == 3
    first = seen[0]
    assert first["messages"][0]["role"] == "system" and first["tool_choice"] == "auto"
    assert "additionalProperties" not in json.dumps(first["tools"])  # stripped for Gemini
    assert first["reasoning_effort"] == "high"
    second = seen[1]["messages"]
    # The assistant turn is echoed verbatim, provider-specific fields included.
    assert second[2]["tool_calls"][0]["extra_content"]["google"]["thought_signature"] == "sig-c1"
    assert second[3]["role"] == "tool" and second[3]["tool_call_id"] == "c1"
    assert "Inbox" in second[3]["content"]
    third = seen[2]["messages"]
    assert third[-2]["role"] == "tool" and third[-2]["tool_call_id"] == "c2"
    assert third[-1]["role"] == "user" and third[-1]["content"][1]["type"] == "image_url"
    assert result.usage["calls"] == 3 and result.usage["cost_usd"] == 0


def test_daily_quota_is_reported_not_retried(config, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    attempts = []

    def handler(request):
        attempts.append(1)
        return httpx.Response(429, text='{"error": {"message": "Quota exceeded: GenerateRequestsPerDayPerProjectPerModel"}}')

    llm = OpenAICompatLLM("gemini")
    llm.http = httpx.Client(transport=httpx.MockTransport(handler))
    result = Worker(config, ScriptedHuman(), llm=llm).run("x")
    assert len(attempts) == 1 and result.error.startswith("QUOTA:") and result.outcome == "incomplete"


def test_missing_key_gives_signup_hint(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    try:
        OpenAICompatLLM("gemini")
    except Exception as e:
        assert "aistudio.google.com" in str(e)
    else:
        raise AssertionError("expected an error")

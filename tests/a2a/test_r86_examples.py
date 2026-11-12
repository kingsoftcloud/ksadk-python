"""Acceptance examples use real model calls without repeated expert dispatch."""
import importlib.util
import json
from pathlib import Path

import pytest


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["sequential", "handoff", "after_reply"])
async def test_adk_example_forces_only_initial_handoff(monkeypatch, mode):
    from google.adk.models.lite_llm import LiteLLMClient

    for name, value in {"OPENAI_MODEL_NAME": "fixture-model", "OPENAI_BASE_URL": "https://fixture.invalid/v1", "OPENAI_API_KEY": "fixture-not-a-key"}.items():
        monkeypatch.setenv(name, value)
    path = Path(__file__).resolve().parent / "fixtures/a2a-r86-adk/a2a_r86_adk/agent.py"
    spec = importlib.util.spec_from_file_location("r86_adk_example", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    captured = []

    async def completion(self, **kwargs):
        captured.append(kwargs)
        return "model-response"

    monkeypatch.setattr(LiteLLMClient, "acompletion", completion)
    payload = '{"text":"proof","request_input":true}'
    messages = [{"role": "user", "content": payload}]
    if mode == "after_reply":
        messages.append({"role": "tool", "content": "expert reply"})
    tools = [] if mode == "sequential" else [{"type": "function", "function": {"name": "expert"}}]
    assert await module.ExpertModelClient().acompletion("fixture", messages, tools) == "model-response"
    assert len(captured) == 1
    if mode == "handoff":
        assert captured[0]["tool_choice"]["function"]["name"] == "expert"
        assert json.dumps({"message": payload}) in captured[0]["messages"][-1]["content"]
        assert len(messages) == 1
    else:
        assert "tool_choice" not in captured[0]
        assert captured[0]["messages"] == messages

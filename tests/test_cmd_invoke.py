from pathlib import Path

import yaml

from ksadk.cli.cmd_invoke import _extract_content, run_invoke_command


class _FakeInvokeClient:
    calls = []

    def __init__(self, *args, **kwargs):
        self.kwargs = kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get_agent(self, agent_id=None, name=None, include_api_key=False):
        self.__class__.calls.append(
            {
                "agent_id": agent_id,
                "name": name,
                "include_api_key": include_api_key,
            }
        )
        return {
            "basic": {
                "agent_id": "ar-demo",
                "name": "demo-agent",
            },
            "quick_access": {
                "public_endpoint": "https://fresh.example.com",
                "api_key": "ak-fresh",
            },
        }


def test_run_invoke_command_refreshes_stale_state_from_remote(monkeypatch, tmp_path: Path):
    state_file = tmp_path / ".agentengine.state"
    state_file.write_text(
        yaml.safe_dump(
            {
                "agent_id": "ar-demo",
                "name": "demo-agent",
                "endpoint": "http://stale.example.com",
                "api_key": None,
            }
        ),
        encoding="utf-8",
    )

    captured = {}

    async def _fake_invoke_once(endpoint, message, api_key, session_id, stream, insecure, model):
        captured["endpoint"] = endpoint
        captured["api_key"] = api_key
        captured["message"] = message

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("ksadk.api.AgentEngineClient", _FakeInvokeClient)
    monkeypatch.setattr("ksadk.cli.cmd_invoke._invoke_once", _fake_invoke_once)

    run_invoke_command(
        agent_ref=None,
        agent_option=None,
        endpoint=None,
        api_key=None,
        message="hello",
        session=None,
        region="pre-online",
        local=False,
        insecure=False,
        model=None,
        show_thinking=False,
    )

    state = yaml.safe_load(state_file.read_text(encoding="utf-8"))
    assert captured["endpoint"] == "https://fresh.example.com"
    assert captured["api_key"] == "ak-fresh"
    assert state["endpoint"] == "https://fresh.example.com"
    assert state["api_key"] == "ak-fresh"
    assert _FakeInvokeClient.calls[-1] == {
        "agent_id": "ar-demo",
        "name": None,
        "include_api_key": True,
    }


def test_run_invoke_command_persists_generated_session_id(monkeypatch, tmp_path: Path):
    captured_sessions = []

    async def _fake_invoke_once(endpoint, message, api_key, session_id, stream, insecure, model):
        captured_sessions.append(session_id)

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("ksadk.cli.cmd_invoke._invoke_once", _fake_invoke_once)

    run_invoke_command(
        agent_ref=None,
        agent_option=None,
        endpoint=None,
        api_key=None,
        message="hello",
        session=None,
        region="pre-online",
        local=True,
        insecure=False,
        model=None,
        show_thinking=False,
    )

    state_file = tmp_path / ".agentengine.state"
    state = yaml.safe_load(state_file.read_text(encoding="utf-8"))
    assert captured_sessions[0]
    assert state["session_id"] == captured_sessions[0]

    run_invoke_command(
        agent_ref=None,
        agent_option=None,
        endpoint=None,
        api_key=None,
        message="continue",
        session=None,
        region="pre-online",
        local=True,
        insecure=False,
        model=None,
        show_thinking=False,
    )

    assert captured_sessions[1] == captured_sessions[0]


def test_extract_content_supports_response_output_text_delta():
    content, reasoning = _extract_content(
        {
            "_event": "response.output_text.delta",
            "delta": "你好",
        }
    )

    assert content == "你好"
    assert reasoning == ""


def test_extract_content_supports_response_reasoning_delta():
    content, reasoning = _extract_content(
        {
            "_event": "response.reasoning.delta",
            "delta": "先分析一下",
        }
    )

    assert content == ""
    assert reasoning == "先分析一下"


def test_extract_content_ignores_response_completed_payload():
    content, reasoning = _extract_content(
        {
            "_event": "response.completed",
            "output_text": "最终答案",
        }
    )

    assert content == ""
    assert reasoning == ""

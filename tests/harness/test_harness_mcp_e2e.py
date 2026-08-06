from __future__ import annotations

import time

import httpx
import pytest

from ksadk.harness import HarnessApp
from ksadk.harness.runner import HarnessReasoningTurn, HarnessToolCall

from .fixtures.mcp_server import run_fixture_mcp_server, run_http_app


class _CallLookupThenAnswer:
    def __init__(self) -> None:
        self.seen_tools: list[str] = []

    async def complete(self, *, model, prompt, messages, tools):
        del model, prompt
        self.seen_tools = [tool.name for tool in tools]
        tool_results = [message for message in messages if message.get("role") == "tool"]
        if not tool_results:
            return HarnessReasoningTurn(
                tool_calls=(
                    HarnessToolCall(
                        call_id="lookup-1",
                        name="weather_lookup",
                        arguments={"value": "beijing"},
                    ),
                )
            )
        return HarnessReasoningTurn(final_text=f"MCP result: {tool_results[-1]['content']}")


@pytest.mark.asyncio
async def test_real_mcp_transport_filter_prefix_and_credential(tmp_path, capsys):
    started = time.monotonic()
    with run_fixture_mcp_server(label="sunny") as fixture:
        config_path = tmp_path / "harness.yaml"
        config_path.write_text(
            "\n".join(
                [
                    "model: glm-5.2",
                    "prompt: use the weather tool",
                    "mcp_tools:",
                    "  - name: weather",
                    f"    url: {fixture.url}",
                    "    api_key: harness-secret",
                    "    tool_filter: [lookup]",
                    "    tool_name_prefix: weather",
                    "sandbox:",
                    "  read_only: true",
                ]
            ),
            encoding="utf-8",
        )
        reasoner = _CallLookupThenAnswer()
        app = HarnessApp.from_yaml(config_path, reasoner=reasoner, workspace_root=tmp_path)
        fastapi_app = app.build_app()
        with run_http_app(fastapi_app) as harness_url:
            startup_seconds = time.monotonic() - started
            async with httpx.AsyncClient(base_url=harness_url, trust_env=False) as client:
                response = await client.post(
                    "/v1/chat/completions",
                    json={
                        "model": "glm-5.2",
                        "messages": [{"role": "user", "content": "weather?"}],
                    },
                    timeout=10,
                )
            first_tool_call_seconds = time.monotonic() - started

    evidence = (
        f"startup_seconds={startup_seconds:.3f} "
        f"first_tool_call_seconds={first_tool_call_seconds:.3f} "
        "mcp_tool=weather_lookup result=sunny:beijing"
    )
    with capsys.disabled():
        print(evidence)
    assert response.status_code == 200, response.text
    assert "sunny:beijing" in response.json()["choices"][0]["message"]["content"]
    assert reasoner.seen_tools == [
        "sandbox_read_file",
        "sandbox_run_command",
        "weather_lookup",
    ]
    assert fixture.log.calls == [("lookup", "beijing")]
    assert "forbidden" not in reasoner.seen_tools
    assert fixture.log.authorization
    assert set(fixture.log.authorization) == {"Bearer harness-secret"}
    assert startup_seconds < 60
    assert first_tool_call_seconds < 60
    assert "startup_seconds=" in evidence
    assert "first_tool_call_seconds=" in evidence


@pytest.mark.asyncio
async def test_mcp_startup_failure_identifies_server(tmp_path):
    app = HarnessApp.from_yaml(
        _write_config(tmp_path, "http://127.0.0.1:1/mcp", api_key="bad"),
        reasoner=_CallLookupThenAnswer(),
        workspace_root=tmp_path,
    )
    runner = app.build_runner()
    with pytest.raises(RuntimeError, match="weather.*127.0.0.1:1"):
        await runner.invoke({"input": "weather?"})


@pytest.mark.asyncio
async def test_missing_filtered_tool_identifies_server_and_tool(tmp_path):
    with run_fixture_mcp_server() as fixture:
        path = _write_config(tmp_path, fixture.url, api_key="harness-secret")
        path.write_text(
            path.read_text(encoding="utf-8").replace(
                "tool_filter: [lookup]", "tool_filter: [missing_tool]"
            ),
            encoding="utf-8",
        )
        app = HarnessApp.from_yaml(
            path,
            reasoner=_CallLookupThenAnswer(),
            workspace_root=tmp_path,
        )
        with pytest.raises(RuntimeError, match="weather.*missing_tool"):
            await app.build_runner().invoke({"input": "weather?"})


def _write_config(tmp_path, url: str, *, api_key: str):
    path = tmp_path / "harness.yaml"
    path.write_text(
        "\n".join(
            [
                "model: glm-5.2",
                "prompt: p",
                "mcp_tools:",
                "  - name: weather",
                f"    url: {url}",
                f"    api_key: {api_key}",
                "    tool_filter: [lookup]",
                "    tool_name_prefix: weather",
            ]
        ),
        encoding="utf-8",
    )
    return path

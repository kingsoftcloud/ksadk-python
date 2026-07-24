"""HarnessApp skeleton 测试 (goal-08)。

- 从 yaml 配置起到可调用一次成功(≤60s,H2 验收指标)。
- 与普通 runtime app 行为一致(共用 create_runtime_app factory;数据面 route group,
  控制面不进)。
- yaml 最小子集外字段 → 明确"暂不支持"错误(不静默忽略)。
- per-invocation override(model/prompt)生效;sandbox 默认 read-only。
- 基础 plugin host 钩子被调用。
"""

from __future__ import annotations

import time

import httpx
import pytest

from ksadk.harness import HarnessApp, HarnessConfig, HarnessConfigError
from ksadk.harness.runner import HarnessReasoningTurn
from ksadk.runtime.adapter import StartRequest


@pytest.fixture(autouse=True)
async def _reset_session_service_cache():
    """测试隔离:harness 测试会经 conversation 机制触发 ``resolve_session_service``
    全局缓存(默认 local 后端),若不清理会架空后续测试(如 background_run 的
    ``KSADK_SESSION_BACKEND=memory``)。每个 harness 测试前后重置缓存。"""
    from ksadk.sessions import reset_session_service

    await reset_session_service()
    yield
    await reset_session_service()


VALID_YAML = """
model: glm-5.1
prompt: 你是助手
mcp_tools:
  - name: weather
    url: http://mcp.example/mcp
    tool_filter: [forecast]
sandbox:
  read_only: true
"""

MINIMAL_YAML = """
model: glm-5.1
prompt: 你是助手
sandbox:
  read_only: true
"""


class _FinalReasoner:
    async def complete(self, *, model, prompt, messages, tools):
        del tools
        return HarnessReasoningTurn(final_text=f"[{model}] {prompt}: {messages[1]['content']}")


def _write(tmp_path, text: str):
    path = tmp_path / "harness.yaml"
    path.write_text(text, encoding="utf-8")
    return path


# ---- 子集校验 ----


def test_from_yaml_minimal_subset(tmp_path):
    app = HarnessApp.from_yaml(_write(tmp_path, VALID_YAML))
    config = app.config
    assert config.model == "glm-5.1"
    assert config.prompt == "你是助手"
    assert len(config.mcp_tools) == 1
    assert config.mcp_tools[0].name == "weather"
    assert config.sandbox.read_only is True


def test_sandbox_default_read_only(tmp_path):
    app = HarnessApp.from_yaml(_write(tmp_path, "model: m\nprompt: p\n"))
    assert app.config.sandbox.read_only is True  # 默认 read-only


def test_writable_sandbox_is_explicitly_unsupported(tmp_path):
    with pytest.raises(HarnessConfigError, match="read_only=false.*暂不支持"):
        HarnessApp.from_yaml(
            _write(tmp_path, "model: m\nprompt: p\nsandbox:\n  read_only: false\n")
        )


@pytest.mark.parametrize("field", ["memory", "knowledge", "workflow", "tracing"])
def test_out_of_subset_fields_rejected(tmp_path, field):
    with pytest.raises(HarnessConfigError, match="暂不支持|不支持"):
        HarnessApp.from_yaml(_write(tmp_path, f"model: m\nprompt: p\n{field}: x\n"))


def test_unknown_field_rejected(tmp_path):
    with pytest.raises(HarnessConfigError, match="未知字段"):
        HarnessApp.from_yaml(_write(tmp_path, "model: m\nprompt: p\nbogus: 1\n"))


def test_missing_required_field_rejected(tmp_path):
    with pytest.raises(HarnessConfigError):
        HarnessApp.from_yaml(_write(tmp_path, "model: m\n"))  # 缺 prompt


# ---- 从配置到可调用一次(happy path, ≤60s) ----


@pytest.mark.asyncio
async def test_yaml_to_callable_under_60s(tmp_path):
    started = time.monotonic()
    app = HarnessApp.from_yaml(
        _write(tmp_path, MINIMAL_YAML),
        reasoner=_FinalReasoner(),
        workspace_root=tmp_path,
    )
    fastapi_app = app.build_app()
    transport = httpx.ASGITransport(app=fastapi_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://harness") as client:
        response = await client.post(
            "/v1/chat/completions",
            json={"model": "glm-5.1", "messages": [{"role": "user", "content": "你好"}]},
        )
    elapsed = time.monotonic() - started
    assert response.status_code == 200, response.text
    body = response.json()
    text = str(body)
    assert "glm-5.1" in text and "你是助手" in text and "你好" in text
    assert elapsed < 60  # H2 验收指标(本地基准)


@pytest.mark.asyncio
async def test_data_plane_only_no_control_plane(tmp_path):
    """HarnessApp 只挂数据面 route group;控制面(cancel/builder/debug)不进。"""
    app = HarnessApp.from_yaml(_write(tmp_path, VALID_YAML))
    fastapi_app = app.build_app()
    paths = {getattr(route, "path", None) for route in fastapi_app.routes}
    # 数据面在
    assert "/health" in paths
    assert "/v1/chat/completions" in paths
    # 控制面不在
    assert "/agentengine/api/v1/CancelRun" not in paths
    assert "/builder/save" not in paths
    assert "/traces" not in paths


@pytest.mark.asyncio
async def test_health_consistent_with_normal_app(tmp_path):
    """与普通 runtime app 行为一致(共用 factory):/health 在 harness app 同样可用。"""
    app = HarnessApp.from_yaml(_write(tmp_path, VALID_YAML))
    fastapi_app = app.build_app()
    transport = httpx.ASGITransport(app=fastapi_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://harness") as client:
        response = await client.get("/health")
    assert response.status_code == 200


# ---- override ----


@pytest.mark.asyncio
async def test_per_invocation_override(tmp_path):
    app = HarnessApp.from_yaml(
        _write(tmp_path, MINIMAL_YAML),
        reasoner=_FinalReasoner(),
        workspace_root=tmp_path,
    )
    handle = await app.start(
        StartRequest(input="x", user_id="u", session_id="s"),
        overrides={"model": "glm-override", "prompt": "覆盖"},
    )
    events = [event async for event in app.stream(handle)]
    text = [event for event in events if event.event_type == "text.completed"][-1]
    assert "glm-override" in str(text.payload) and "覆盖" in str(text.payload)


def test_override_out_of_subset_rejected(tmp_path):
    app = HarnessApp.from_yaml(_write(tmp_path, VALID_YAML))
    with pytest.raises(ValueError, match="override"):
        app.build_runner(overrides={"memory": "x"})


# ---- yaml → RuntimeAdapter start(request) 映射 ----


@pytest.mark.asyncio
async def test_yaml_to_runtime_adapter_start(tmp_path):
    app = HarnessApp.from_yaml(_write(tmp_path, VALID_YAML))
    handle = await app.start(StartRequest(input="hi", user_id="u", session_id="s"))
    assert handle.run_id
    assert handle.session_id == "s"
    assert handle.runtime_type == "harness"


@pytest.mark.asyncio
async def test_app_owns_one_adapter_and_start_override_is_request_local(tmp_path):
    """The app's adapter is the one used by start and the deployed app."""
    app = HarnessApp(
        HarnessConfig(model="base", prompt="base-prompt"),
        reasoner=_FinalReasoner(),
        workspace_root=tmp_path,
    )
    adapter = app.adapter()
    assert app.adapter() is adapter

    handle = await app.start(
        StartRequest(
            input="hello",
            user_id="u",
            session_id="s",
            model="request-model",
            config={"prompt": "request-prompt"},
        ),
    )
    events = [event async for event in adapter.stream(handle)]
    text_events = [event for event in events if event.event_type == "text.completed"]
    assert text_events
    assert "request-model" in str(text_events[-1].payload)
    assert "request-prompt" in str(text_events[-1].payload)

    fastapi_app = app.build_app()
    assert fastapi_app.state.runtime.runtime_adapter is adapter
    assert fastapi_app.state.runtime.runner is app.runner


@pytest.mark.asyncio
async def test_cancel_and_stream_use_the_same_owned_adapter(tmp_path):
    app = HarnessApp(
        HarnessConfig(model="m", prompt="p"),
        reasoner=_FinalReasoner(),
        workspace_root=tmp_path,
    )
    handle = await app.start(StartRequest(input="hello", user_id="u", session_id="s"))
    result = await app.cancel(handle)
    assert result.value == "pending_cancel_recorded"
    events = [event async for event in app.stream(handle)]
    assert [event.event_type for event in events] == ["run.canceled"]


@pytest.mark.asyncio
async def test_read_only_harness_does_not_mount_mutation_or_terminal_routes(tmp_path):
    app = HarnessApp(
        HarnessConfig(model="m", prompt="p"),
        reasoner=_FinalReasoner(),
        workspace_root=tmp_path,
    )
    fastapi_app = app.build_app()
    paths = {getattr(route, "path", "") for route in fastapi_app.routes}
    assert "/agentengine/api/v1/UploadFile" not in paths
    assert "/agentengine/api/v1/AddWorkspaceFile" not in paths
    assert "/agentengine/api/v1/DeleteWorkspaceFile" not in paths
    assert "/_ksadk/terminal/ws" not in paths
    assert "/agentengine/agui" not in paths
    assert app.capabilities.responses is True
    assert app.capabilities.sessions is True
    assert app.capabilities.files is False
    assert app.capabilities.a2a is False
    assert app.capabilities.agui is False


@pytest.mark.asyncio
async def test_harness_apps_have_distinct_session_services(tmp_path):
    first = HarnessApp(HarnessConfig(model="m", prompt="p"), workspace_root=tmp_path / "one")
    second = HarnessApp(HarnessConfig(model="m", prompt="p"), workspace_root=tmp_path / "two")
    first_app = first.build_app()
    second_app = second.build_app()
    assert first_app.state.runtime.session_service is not second_app.state.runtime.session_service


@pytest.mark.asyncio
async def test_harness_http_sessions_are_app_scoped(tmp_path):
    first = HarnessApp(
        HarnessConfig(model="m", prompt="p"), workspace_root=tmp_path / "one"
    ).build_app()
    second = HarnessApp(
        HarnessConfig(model="m", prompt="p"), workspace_root=tmp_path / "two"
    ).build_app()
    payload = {"sessionId": "same-session"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=first), base_url="http://first"
    ) as client:
        created = await client.post("/apps/harness/users/u/sessions", json=payload)
        assert created.status_code == 200, created.text
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=second), base_url="http://second"
    ) as client:
        missing = await client.get("/apps/harness/users/u/sessions/same-session")
        assert missing.status_code == 404
        bootstrap = await client.post(
            "/agentengine/api/v1/GetAgentUiBootstrap", json={"AgentId": "harness"}
        )
        assert bootstrap.status_code == 200, bootstrap.text
        assert bootstrap.json()["Data"]["SessionBackend"]["Backend"] == "memory"


# ---- plugin host ----


def test_plugin_hooks_called(tmp_path):
    calls: list[str] = []

    class _Plugin:
        def before_build(self, config):
            calls.append("before")

        def after_build(self, app, config):
            calls.append("after")

    app = HarnessApp.from_yaml(_write(tmp_path, VALID_YAML), plugins=[_Plugin()])
    app.build_app()
    assert calls == ["before", "after"]

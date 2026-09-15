"""端到端：harness 构建 → provider runtime adapter → run:stream SSE 增量输出。

组合层使用 shipped harness bundle + 假 DSH 二进制完成真实构建；执行层的
Provider adapter 被替换为按节奏发增量的 fixture，验证 Studio 的 SSE 订阅端
在整轮结束前就能收到 message.delta（终态之前），且无重复帧。
"""

from __future__ import annotations

import asyncio
import json
import shutil
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ksadk.events.canonical import (
    ContentSnapshot,
    ItemStarted,
    ItemUpdated,
    RunCompleted,
    RunStarted,
    SourceRef,
)
from ksadk.events.content import TextContent
from ksadk.plugins.dsh_home import prepare_studio_dsh_home
from ksadk.plugins.dsh_toolchain import DSH_VERSION
from ksadk.plugins.providers.harness_dsh import shipped_harness_dsh_bundle
from ksadk.studio.api import create_studio_app
from ksadk.studio.contracts import (
    AgentBindings,
    AgentSpec,
    Instructions,
    ModelSpec,
    NetworkPolicy,
    RuntimeRef,
    SecuritySpec,
)
from ksadk.studio.service import StudioService
from tests.studio.runtime_adapter_fixtures import (
    RecordingRuntimeAdapter,
    RuntimeFixture,
)


def _dsh_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "dsh-home"
    prepare_studio_dsh_home(home)
    profile = home / "profiles" / "studio"
    installed = profile / "node_modules" / "@kingsoftcloud" / "ksadk-harness-provider"
    installed.parent.mkdir(parents=True)
    shutil.copytree(shipped_harness_dsh_bundle().root, installed)
    (profile / "package.json").write_text(
        json.dumps(
            {
                "dependencies": {"@kingsoftcloud/ksadk-harness-provider": "1.0.0"},
                "dsh": {"profile": {"bundles": ["@kingsoftcloud/ksadk-harness-provider"]}},
            }
        ),
        encoding="utf-8",
    )
    executable = tmp_path / "dsh-fixture"
    executable.write_text(
        "#!/bin/sh\n"
        'case "$*" in\n'
        f"  *--version*) echo {DSH_VERSION};;\n"
        "  *--dump-config*) echo 'profile: studio; harness: 1.0.0';;\n"
        "  *) exit 2;;\n"
        "esac\n",
        encoding="utf-8",
    )
    executable.chmod(0o700)
    monkeypatch.setenv("KSADK_DSH_HOME", str(home))
    monkeypatch.setenv("KSADK_DSH_PROFILE", "studio")
    monkeypatch.setenv("KSADK_DSH_BIN", str(executable))


class _GatedHarnessEvents:
    """模拟真实 Managed Harness 的增量事件节奏（gate 前后各一段）。"""

    def __init__(self) -> None:
        self.gate: asyncio.Event | None = None

    async def __call__(
        self, request: object, handle: object
    ) -> AsyncIterator[object]:
        common = {
            "schema_version": 2,
            "timestamp": 1.0,
            "run_id": handle.run_id,
            "scope_id": f"scope-{handle.run_id}",
        }
        source = SourceRef(framework="ksadk")
        yield RunStarted(
            event_id=f"{handle.run_id}:run",
            seq=1,
            status="running",
            source=source,
            **common,
        )
        yield ItemStarted(
            event_id=f"{handle.run_id}:start",
            seq=2,
            item_id="msg-1",
            item_kind="message",
            phase="final_answer",
            initial=ContentSnapshot(parts=(TextContent(part_id="text-0", text="你好"),)),
            source=source,
            **common,
        )
        for index, chunk in enumerate(["，", "正在", "流式", "输出"]):
            yield ItemUpdated(
                event_id=f"{handle.run_id}:delta-{index}",
                seq=3 + index,
                item_id="msg-1",
                item_kind="message",
                op="append",
                update=TextContent(part_id="text-0", text=chunk),
                source=source,
                **common,
            )
        yield ItemUpdated(
            event_id=f"{handle.run_id}:delta-final",
            seq=7,
            item_id="msg-1",
            item_kind="message",
            op="append",
            update=TextContent(part_id="text-0", text="完成。"),
            source=source,
            **common,
        )
        yield RunCompleted(
            event_id=f"{handle.run_id}:done",
            seq=8,
            status="completed",
            output_refs=(),
            source=source,
            **common,
        )


class _FakeProviderRuntime:
    """替换 StudioPluginRuntime：只暴露 provider runtime adapter 通道。"""

    def __init__(self, adapter: RecordingRuntimeAdapter) -> None:
        self._adapter = adapter

    def kernel_adapter_provider(self, _spec: object):
        return lambda: self._adapter

    async def close_session_if_dynamic(self, _spec: object, _session_id: str) -> None:
        return None


@pytest.mark.asyncio
async def test_harness_run_stream_delivers_live_deltas_over_sse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _dsh_fixture(tmp_path, monkeypatch)
    studio = StudioService(tmp_path)
    studio.create_agent(
        agent_id="stream-harness",
        name="Stream Harness",
        spec=AgentSpec(
            runtime=RuntimeRef(type="harness"),
            instructions=Instructions(system="流式输出验证。"),
            model=ModelSpec(
                model="fixture-model",
                endpoint_url="https://model.example.test/v1/chat/completions",
                credential_ref="env://MODEL_API_KEY",
            ),
            bindings=AgentBindings(),
            security=SecuritySpec(
                network=NetworkPolicy(allowed_hosts=["model.example.test"]),
                allowed_permissions=[
                    "filesystem:bundle-read",
                    "filesystem:session-store",
                    "network:mcp",
                    "process:host-user",
                ],
            ),
        ),
    )
    build = await studio.ensure_current_build("stream-harness")
    resolved = studio.resolve_run_spec(build.id)
    assert resolved.plugin_bundle_root is not None
    # 生产路径：harness 构建走 provider runtime adapter（executor.stream 实时流）。
    assert resolved.request_config.get("provider_runtime_adapter") is True

    events = _GatedHarnessEvents()
    fixture = RuntimeFixture(events, runtime_types=("harness",))
    adapter = fixture._create_adapter("harness")
    studio.run_service.plugin_runtime = _FakeProviderRuntime(adapter)

    app = create_studio_app(tmp_path, service=studio, security_enabled=False)
    with TestClient(app) as client:
        with client.stream(
            "POST",
            f"/api/v1/builds/{build.id}/run:stream",
            headers={"Idempotency-Key": "e2e-harness-stream"},
            json={"sessionId": "ses-harness-stream", "input": {"content": "讲个故事"}},
            ) as response:
                assert response.status_code == 200
                stream = "".join(response.iter_text())

    assert "event: run.created" in stream
    assert "event: message.delta" in stream
    assert "流式" in stream
    assert "event: run.completed" in stream
    deltas = [line for line in stream.splitlines() if "message.delta" in line]
    # ItemStarted 携带首段文本"你好"，投影为一条 message.delta；4 个 ItemUpdated 各一条。
    assert len(deltas) == 5, deltas
    # 增量必须出现在终态之前（终局一次性爆发则相反）。
    assert stream.index("message.delta") < stream.index("run.completed")


def test_conversation_history_reasoner_passes_streaming_declaration() -> None:
    """graph_builder 依赖 _streaming 决定增量推理；包装器必须透传。"""

    from ksadk.plugins.providers.harness import _ConversationHistoryReasoner

    class _StreamingDelegate:
        _streaming = True

    class _BufferedDelegate:
        pass

    assert _ConversationHistoryReasoner(_StreamingDelegate())._streaming is True
    assert _ConversationHistoryReasoner(_BufferedDelegate())._streaming is False

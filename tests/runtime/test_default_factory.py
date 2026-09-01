"""Codex、ADK、LangGraph 默认 RuntimeAdapter Factory。"""

from __future__ import annotations

import errno
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import ksadk.runtime as runtime_api
from ksadk.codex.client import CodexClient
from ksadk.codex.runtime import CodexRuntimeAdapter
from ksadk.harness.runtime import HarnessRuntimeAdapter
from ksadk.runners.base_runner import BaseRunner
from ksadk.runtime import ADKRuntimeAdapter, LangGraphRuntimeAdapter
from ksadk.runtime import factory as runtime_factory


class _FactoryCodexClient(CodexClient):
    async def start_thread(self, config=None) -> str:
        return "thread-1"

    def run_turn(
        self,
        thread_id: str,
        prompt: Any,
        *,
        config: dict[str, Any] | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        async def events():
            if False:
                yield {}

        return events()

    async def interrupt_active_turn(self, thread_id: str) -> bool:
        return False

    async def resume_thread(self, thread_id: str, config=None) -> str:
        return thread_id

    async def close(self) -> None:
        return None


class _FactoryRunner(BaseRunner):
    def load_agent(self) -> None:
        return None

    async def invoke(self, input_data: dict[str, Any]) -> dict[str, Any]:
        return {"output": "done"}

    async def stream(self, input_data: dict[str, Any]):
        if False:
            yield {}


@pytest.mark.parametrize(
    ("runtime_type", "adapter_type"),
    [
        ("codex", CodexRuntimeAdapter),
        ("harness", HarnessRuntimeAdapter),
        ("adk", ADKRuntimeAdapter),
        ("langgraph", LangGraphRuntimeAdapter),
    ],
)
def test_default_registry_creates_expected_adapter_from_launch_context(
    tmp_path: Path,
    runtime_type: str,
    adapter_type: type,
) -> None:
    """防止 Runtime 选择重新散落到 Web、Studio 和协议入口。"""

    runner_calls: list[tuple[object, str]] = []
    client = _FactoryCodexClient()

    def runner_factory(detection: object, project_dir: str) -> BaseRunner:
        runner_calls.append((detection, project_dir))
        return _FactoryRunner(detection, project_dir)

    services = runtime_api.RuntimeServices(
        codex_client_factory=lambda: client,
        runner_factory=runner_factory,
    )
    detection = SimpleNamespace(type=runtime_type)
    context = runtime_api.RuntimeLaunchContext(
        runtime_type=runtime_type,
        project_dir=tmp_path,
        detection=detection,
        config={"model": "model-a", "prompt": "You are a test agent."},
        services=services,
    )

    registry = runtime_api.build_default_runtime_registry()
    adapter = registry.create(context)

    assert isinstance(adapter, adapter_type)
    if runtime_type == "codex":
        assert adapter._client is client
        assert runner_calls == []
    elif runtime_type != "harness":
        assert runner_calls == [(detection, str(tmp_path))]
    else:
        assert runner_calls == []


def test_create_runtime_adapter_uses_canonical_default_registry(tmp_path: Path) -> None:
    """防止便利入口维护第二份 Runtime 分发表。"""

    client = _FactoryCodexClient()
    context = runtime_api.RuntimeLaunchContext(
        runtime_type="codex",
        project_dir=tmp_path,
        services=runtime_api.RuntimeServices(codex_client_factory=lambda: client),
    )

    adapter = runtime_api.create_runtime_adapter(context)

    assert isinstance(adapter, CodexRuntimeAdapter)
    assert adapter._client is client


def test_codex_factory_applies_runtime_sandbox_and_timeout_config(tmp_path: Path) -> None:
    """防止 Factory 丢失 Manifest 已校验过的 Codex 运行参数。"""

    context = runtime_api.RuntimeLaunchContext(
        runtime_type="codex",
        project_dir=tmp_path,
        config={"sandbox_read_only": False, "turn_timeout_seconds": 2.5},
        services=runtime_api.RuntimeServices(
            codex_client_factory=_FactoryCodexClient,
        ),
    )

    adapter = runtime_api.create_runtime_adapter(context)

    assert adapter._sandbox_read_only is False
    assert adapter._turn_timeout_seconds == 2.5


def test_codex_factory_enables_structured_user_input_in_default_mode(tmp_path: Path) -> None:
    captured: dict[str, Any] = {}

    class _ConfiguredClient(_FactoryCodexClient):
        def __init__(self, config=None) -> None:
            captured["config"] = config

    context = runtime_api.RuntimeLaunchContext(
        runtime_type="codex",
        project_dir=tmp_path,
        services=runtime_api.RuntimeServices(codex_client_factory=_ConfiguredClient),
    )

    runtime_api.create_runtime_adapter(context)

    assert "features.default_mode_request_user_input=true" in captured["config"].config_overrides


def test_codex_home_uses_runtime_state_when_code_bundle_is_read_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Managed Code bundles are read-only, but Codex still needs isolated state."""

    project_dir = tmp_path / "read-only-code"
    blocked_home = project_dir / ".agentkit" / "codex-home"
    state_dir = tmp_path / "runtime-state"
    original_mkdir = Path.mkdir

    def reject_bundle_write(path: Path, *args: Any, **kwargs: Any) -> None:
        if path == blocked_home:
            raise OSError(errno.EROFS, "Read-only file system", str(path))
        original_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", reject_bundle_write)
    monkeypatch.delenv("KSADK_CODEX_HOME", raising=False)
    monkeypatch.delenv("KSADK_RUNTIME_STATE_DIR", raising=False)
    monkeypatch.setenv("KSADK_SESSION_PATH", str(state_dir / "sessions.sqlite"))

    assert runtime_factory._isolated_codex_home(project_dir) == state_dir / "codex-home"
    assert (state_dir / "codex-home").is_dir()


def test_framework_factory_requires_detection_without_injected_runner(
    tmp_path: Path,
) -> None:
    context = runtime_api.RuntimeLaunchContext(runtime_type="adk", project_dir=tmp_path)

    with pytest.raises(ValueError, match="detection"):
        runtime_api.create_runtime_adapter(context)

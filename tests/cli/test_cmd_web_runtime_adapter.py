from __future__ import annotations

from pathlib import Path

from ksadk.cli import runtime_bootstrap
from ksadk.runtime import RuntimeExecutor, RuntimeRegistry


class _FrameworkType:
    value = "fixture"


class _Detection:
    type = _FrameworkType()
    name = "fixture-agent"
    raw_config = {"feature": "enabled"}


def test_create_runtime_web_app_uses_executor_and_launch_context(monkeypatch, tmp_path: Path):
    registry = RuntimeRegistry()
    monkeypatch.setattr(runtime_bootstrap, "build_default_runtime_registry", lambda: registry)

    app = runtime_bootstrap.create_runtime_web_app(_Detection(), tmp_path)

    assert isinstance(app.state.runtime.executor, RuntimeExecutor)
    assert app.state.runtime.launch_context.runtime_type == "fixture"
    assert app.state.runtime.launch_context.project_dir == tmp_path
    assert app.state.runtime.launch_context.detection is not None
    assert app.state.runtime.launch_context.config == {"feature": "enabled"}
    assert not hasattr(app.state.runtime, "runner")

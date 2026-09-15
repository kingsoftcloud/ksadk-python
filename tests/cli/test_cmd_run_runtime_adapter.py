from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from ksadk.cli import cmd_run


def test_noninteractive_run_launches_shared_runtime_app(monkeypatch, tmp_path: Path) -> None:
    runtime_app = object()
    launched: dict[str, object] = {}
    detection = SimpleNamespace(
        type=SimpleNamespace(value="langgraph"),
        name="fixture-agent",
    )
    monkeypatch.setattr(
        cmd_run,
        "create_runtime_web_app",
        lambda result, path: runtime_app if (result, path) == (detection, tmp_path) else None,
    )
    monkeypatch.setattr(
        cmd_run.uvicorn,
        "run",
        lambda app, **kwargs: launched.update({"app": app, **kwargs}),
    )

    cmd_run._run_custom(
        detection,
        tmp_path,
        8899,
        False,
        True,
        False,
    )

    assert launched == {
        "app": runtime_app,
        "host": "127.0.0.1",
        "port": 8899,
    }

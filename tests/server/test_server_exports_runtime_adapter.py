from __future__ import annotations

import importlib
from pathlib import Path

from fastapi import FastAPI

import ksadk.server as server
from ksadk.runtime import RuntimeExecutor, RuntimeLaunchContext, RuntimeRegistry


def test_server_package_composes_explicit_runtime_without_global_runner(tmp_path: Path) -> None:
    executor = RuntimeExecutor(RuntimeRegistry())
    context = RuntimeLaunchContext(runtime_type="fixture", project_dir=tmp_path)

    app = server.create_runtime_app(
        server.RuntimeAppConfig(
            runtime_executor=executor,
            launch_context=context,
        ),
        server.configure_runtime_app,
    )

    assert app.state.runtime.executor is executor
    assert app.state.runtime.launch_context is context
    assert not hasattr(server, "set_runner")
    assert not isinstance(getattr(server, "app", None), FastAPI)


def test_server_app_module_is_a_thin_explicit_composition_api() -> None:
    app_module = importlib.import_module("ksadk.server.app")

    assert app_module.create_runtime_app is server.create_runtime_app
    assert app_module.configure_runtime_app is server.configure_runtime_app
    assert not hasattr(app_module, "set_runner")
    assert not hasattr(app_module, "app")

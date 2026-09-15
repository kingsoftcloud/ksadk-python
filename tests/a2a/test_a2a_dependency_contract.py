from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import tomllib

ROOT = Path(__file__).resolve().parents[2]


def test_a2a_core_does_not_install_postgresql_task_store_dependencies() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    core = project["dependencies"]
    extras = project["optional-dependencies"]

    assert "a2a-sdk[fastapi]==1.1.0" in core
    assert not any(
        requirement.startswith("a2a-sdk[") and "postgresql" in requirement for requirement in core
    )
    assert "a2a-postgres" in extras
    assert "a2a-sdk[postgresql]==1.1.0" in extras["a2a-postgres"]
    assert "SQLAlchemy[asyncio]>=2.0.0,<3.0.0" in extras["a2a"]
    assert "aiosqlite>=0.20.0,<1.0.0" in extras["a2a"]


def test_managed_discovery_startup_does_not_require_sqlalchemy() -> None:
    """The real managed entrypoint must start without the v2 database stack."""
    probe = """
import importlib.abc
import sys

class BlockSqlAlchemy(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "sqlalchemy" or fullname.startswith("sqlalchemy."):
            raise ModuleNotFoundError("sqlalchemy intentionally unavailable")
        return None

sys.meta_path.insert(0, BlockSqlAlchemy())

from ksadk.managed_a2a_card import build_managed_a2a_card_if_configured
from ksadk.server.app import RuntimeAppConfig, configure_runtime_app, create_runtime_app

app = create_runtime_app(
    RuntimeAppConfig(a2a=build_managed_a2a_card_if_configured()),
    configure_runtime_app,
)
paths = {getattr(route, "path", None) for route in app.routes}
assert "/.well-known/agent-card.json" in paths
"""
    env = os.environ.copy()
    env["KSADK_A2A_RUNTIME_ID"] = "core-install-runtime"
    completed = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=ROOT,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    assert completed.returncode == 0, completed.stderr

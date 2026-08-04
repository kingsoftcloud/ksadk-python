from __future__ import annotations

from pathlib import Path
import tomllib


ROOT = Path(__file__).resolve().parents[2]


def test_a2a_core_does_not_install_postgresql_task_store_dependencies() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))[
        "project"
    ]
    core = project["dependencies"]
    extras = project["optional-dependencies"]

    assert "a2a-sdk[fastapi]==1.1.0" in core
    assert not any(
        requirement.startswith("a2a-sdk[") and "postgresql" in requirement
        for requirement in core
    )
    assert "a2a-postgres" in extras
    assert "a2a-sdk[postgresql]==1.1.0" in extras["a2a-postgres"]

"""Tests for the current agent loading contract."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from ksadk.runners.utils.loader import load_agent_module


def _write_package(project_dir: Path, package_name: str, module_name: str, content: str) -> str:
    package_dir = project_dir / package_name
    package_dir.mkdir(parents=True, exist_ok=True)
    (package_dir / "__init__.py").write_text("", encoding="utf-8")
    (package_dir / f"{module_name}.py").write_text(content, encoding="utf-8")
    return f"{package_name}/{module_name}.py"


def _cleanup_module(module_name: str) -> None:
    sys.modules.pop(module_name, None)


def test_load_agent_module_returns_root_agent_and_module(tmp_path: Path):
    entry_point = _write_package(
        tmp_path,
        "agent_loader_basic_pkg",
        "agent_impl",
        'root_agent = {"name": "demo-agent", "framework": "langgraph"}\n',
    )
    module_name = "agent_loader_basic_pkg.agent_impl"
    _cleanup_module(module_name)

    agent, module = load_agent_module(str(tmp_path), entry_point, "root_agent")

    assert agent == {"name": "demo-agent", "framework": "langgraph"}
    assert module.__name__ == module_name


def test_load_agent_module_supports_nested_entry_point(tmp_path: Path):
    project_pkg = tmp_path / "agent_loader_nested_pkg"
    nested_pkg = project_pkg / "agents"
    nested_pkg.mkdir(parents=True, exist_ok=True)
    (project_pkg / "__init__.py").write_text("", encoding="utf-8")
    (nested_pkg / "__init__.py").write_text("", encoding="utf-8")
    (nested_pkg / "entry.py").write_text('root_agent = "nested-root-agent"\n', encoding="utf-8")
    module_name = "agent_loader_nested_pkg.agents.entry"
    _cleanup_module(module_name)

    agent, module = load_agent_module(str(tmp_path), "agent_loader_nested_pkg/agents/entry.py", "root_agent")

    assert agent == "nested-root-agent"
    assert module.__name__ == module_name


def test_load_agent_module_raises_when_agent_variable_is_missing(tmp_path: Path):
    entry_point = _write_package(
        tmp_path,
        "agent_loader_missing_attr_pkg",
        "agent_impl",
        'some_other_name = "not-root-agent"\n',
    )
    module_name = "agent_loader_missing_attr_pkg.agent_impl"
    _cleanup_module(module_name)

    with pytest.raises(AttributeError, match="未找到 root_agent"):
        load_agent_module(str(tmp_path), entry_point, "root_agent")

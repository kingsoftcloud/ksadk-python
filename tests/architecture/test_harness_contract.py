"""Harness 冻结契约架构守卫（plan Phase 0 验收）。

HarnessSpec / HarnessState / ExecutionEngine 协议不得 import LangGraph
或任何 Runner 专有类型（plan §4.3/§6.1）。用 AST 扫描，不依赖运行时。
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

#: 契约层模块——引擎无关是硬约束。
CONTRACT_MODULES = (
    ROOT / "ksadk" / "harness" / "spec.py",
    ROOT / "ksadk" / "harness" / "state.py",
    ROOT / "ksadk" / "harness" / "capabilities.py",
    ROOT / "ksadk" / "harness" / "engine" / "base.py",
    ROOT / "ksadk" / "harness" / "engine" / "thread_ids.py",
    ROOT / "ksadk" / "harness" / "engine" / "__init__.py",
    ROOT / "ksadk" / "harness" / "conformance" / "contract.py",
    ROOT / "ksadk" / "harness" / "conformance" / "fixtures.py",
    ROOT / "ksadk" / "harness" / "conformance" / "__init__.py",
)

FORBIDDEN_IMPORT_PREFIXES = (
    "langgraph",
    "langchain",
    "ksadk.runners",
    "openai.agents",
    "google.adk",
)

FORBIDDEN_REVERSE_DEPENDENCY_PREFIXES = ("ksadk.studio",)


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
        elif isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
    return modules


def test_contract_modules_do_not_import_engine_frameworks() -> None:
    for path in CONTRACT_MODULES:
        assert path.exists(), f"契约模块缺失: {path}"
        for module in _imported_modules(path):
            for prefix in FORBIDDEN_IMPORT_PREFIXES:
                assert not module.startswith(prefix), (
                    f"{path.relative_to(ROOT)} import 了引擎框架 {module}——"
                    "契约层必须引擎无关（plan §4.3）"
                )


def test_engine_langgraph_is_the_only_langgraph_entrypoint() -> None:
    """LangGraph 只允许出现在 engine/langgraph.py（Phase 1 落地后）。"""
    harness_root = ROOT / "ksadk" / "harness"
    offenders: list[str] = []
    for path in harness_root.rglob("*.py"):
        if path.name == "langgraph.py":
            continue
        for module in _imported_modules(path):
            if module.startswith("langgraph"):
                offenders.append(str(path.relative_to(ROOT)))
    assert not offenders, f"非 engine/langgraph.py 的 harness 模块 import 了 langgraph: {offenders}"


def test_harness_never_depends_on_studio() -> None:
    """Harness is an SDK/runtime layer; Studio may depend on it, never vice versa."""
    harness_root = ROOT / "ksadk" / "harness"
    offenders: list[tuple[str, str]] = []
    for path in harness_root.rglob("*.py"):
        for module in _imported_modules(path):
            if module.startswith(FORBIDDEN_REVERSE_DEPENDENCY_PREFIXES):
                offenders.append((str(path.relative_to(ROOT)), module))
    assert not offenders, f"Harness 反向依赖 Studio: {offenders}"


def test_harness_spec_rejects_floating_refs_by_contract() -> None:
    """Spec 层引用校验来自 resource_ref（版本固定），冒烟验证接线。"""
    from ksadk.harness.spec import ModelBinding

    try:
        ModelBinding(profile_ref="model-profile://kimi-k3")  # 无版本
    except Exception as exc:  # noqa: BLE001
        assert "version" in str(exc).lower() or "版本" in str(exc)
    else:
        raise AssertionError("无版本引用必须被拒绝（不允许 floating latest）")

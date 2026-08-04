"""统一 Runtime 执行架构边界。

这些测试检查模块依赖关系和公开类型，不依赖源码字符串或实现细节。
"""

from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = ROOT / "ksadk"
PRODUCT_AND_PROTOCOL_PACKAGES = ("server", "studio", "agui", "a2a", "harness")
RUNTIME_ENTRYPOINTS = (
    PACKAGE_ROOT / "cli" / "cmd_web.py",
    PACKAGE_ROOT / "cli" / "cmd_run.py",
    PACKAGE_ROOT / "cli" / "cmd_a2a.py",
)
RETIRED_RUNTIME_MODULES = (
    PACKAGE_ROOT / "runners" / "codex_runner.py",
    PACKAGE_ROOT / "studio" / "codex_runtime.py",
    PACKAGE_ROOT / "studio" / "runtime_orchestrator.py",
)


def _imports_runner_layer(path: Path) -> bool:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module == "ksadk.runners" or module.startswith("ksadk.runners."):
                return True
        elif isinstance(node, ast.Import):
            if any(
                alias.name == "ksadk.runners" or alias.name.startswith("ksadk.runners.")
                for alias in node.names
            ):
                return True
    return False


def test_product_protocol_and_web_entrypoints_depend_on_runtime_not_runners() -> None:
    """防止产品/协议入口绕过 RuntimeAdapter 再次依赖 Runner。"""

    candidates = list(RUNTIME_ENTRYPOINTS)
    for package in PRODUCT_AND_PROTOCOL_PACKAGES:
        candidates.extend((PACKAGE_ROOT / package).rglob("*.py"))

    violations = sorted(
        str(path.relative_to(ROOT))
        for path in candidates
        if _imports_runner_layer(path)
    )

    assert violations == []


def test_duplicate_codex_and_studio_runtime_modules_are_removed() -> None:
    """防止已统一的执行链重新出现第二套 Codex/Studio Runtime。"""

    remaining = sorted(
        str(path.relative_to(ROOT)) for path in RETIRED_RUNTIME_MODULES if path.exists()
    )

    assert remaining == []

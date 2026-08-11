"""Maintainability guard for production Python source modules."""

from __future__ import annotations

import warnings
from pathlib import Path

_WARN_LINES = 700
_FAIL_LINES = 1000
_LEGACY_OVERSIZED_MAX = {
    # Reviewed integration baselines. Any further growth still fails this guard;
    # these modules should be split when their current feature seams next change.
    "ksadk/a2a/space_client.py": 1109,
    "ksadk/api/client.py": 2288,
    "ksadk/builders/code_builder.py": 2076,
    "ksadk/cli/cmd_create.py": 2084,
    "ksadk/cli/cmd_files.py": 1266,
    "ksadk/cli/cmd_hermes.py": 1388,
    "ksadk/cli/cmd_invoke.py": 1565,
    "ksadk/cli/cmd_mcp.py": 1213,
    "ksadk/cli/cmd_openclaw.py": 4178,
    "ksadk/codex/client.py": 1042,
    "ksadk/deployment/providers/serverless.py": 1296,
    "ksadk/runners/adk_runner.py": 2219,
    "ksadk/runners/langgraph_runner.py": 1250,
    "ksadk/sessions/local_service.py": 1080,
    "ksadk/sessions/postgres_service.py": 1019,
    "ksadk/studio/api.py": 1014,
    "ksadk/studio/resource_catalog.py": 1217,
    "ksadk/studio/service.py": 1015,
    "ksadk/toolsets/workspace.py": 1028,
    "ksadk/tui/loop.py": 1925,
}


def test_production_python_modules_remain_reviewable() -> None:
    root = Path(__file__).parents[2] / "ksadk"
    oversized: list[str] = []
    large: list[str] = []
    for path in sorted(root.rglob("*.py")):
        line_count = len(path.read_text(encoding="utf-8").splitlines())
        relative = path.relative_to(root.parent).as_posix()
        legacy_limit = _LEGACY_OVERSIZED_MAX.get(relative)
        if line_count > _FAIL_LINES and (
            legacy_limit is None or line_count > legacy_limit
        ):
            oversized.append(f"{relative}: {line_count}")
        elif line_count > _WARN_LINES:
            large.append(f"{relative}: {line_count}")
    if large:
        warnings.warn(
            "Python 模块已超过 700 行，请在继续扩张前说明保留理由或拆分：\n"
            + "\n".join(large),
            stacklevel=1,
        )
    assert not oversized, (
        "以下生产 Python 模块超过 1000 行，必须先按职责拆分：\n"
        + "\n".join(oversized)
    )

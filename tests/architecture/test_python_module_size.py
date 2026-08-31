"""Maintainability guard for production Python source modules."""

from __future__ import annotations

import warnings
from pathlib import Path

_WARN_LINES = 700
_FAIL_LINES = 1000
_LEGACY_OVERSIZED_MAX = {
    # Reviewed integration baselines. Any further growth still fails this guard;
    # these modules should be split when their current feature seams next change.
    "ksadk/agui/agent.py": 1056,
    "ksadk/a2a/control_plane.py": 1060,
    "ksadk/a2a/space_client.py": 1122,
    "ksadk/api/client.py": 2778,
    # Current master already contains the 2,287-line builder integration; PCM
    # adds only its launch-context projection seam. Keep any further growth red.
    "ksadk/builders/code_builder.py": 2371,
    "ksadk/cli/cmd_create.py": 2084,
    "ksadk/cli/cmd_files.py": 1270,
    "ksadk/cli/cmd_hermes.py": 1441,
    "ksadk/cli/cmd_invoke.py": 1565,
    "ksadk/cli/cmd_mcp.py": 1213,
    "ksadk/cli/cmd_openclaw.py": 4218,
    "ksadk/codex/client.py": 1192,
    "ksadk/codex/runtime.py": 1084,
    "ksadk/deployment/providers/serverless.py": 1358,
    # Agent Runtime v2 Phase 1 delivery baseline. These files contain the
    # frozen contract/store implementations; any post-0.8.2 growth stays red
    # until the corresponding responsibility is extracted.
    "ksadk/kernel/bootstrap.py": 1074,
    "ksadk/kernel/ingress.py": 1123,
    "ksadk/kernel/memory_store.py": 1134,
    "ksadk/kernel/postgres_store.py": 1757,
    "ksadk/kernel/sqlite_store.py": 1411,
    "ksadk/runners/adk_runner.py": 2219,
    # 0.8.1 approval continuation baseline; split the LangGraph execution
    # paths at the next runner-focused maintenance pass.
    "ksadk/runners/langgraph_runner.py": 1378,
    # 0.8.1 canonical ToolGateway approval projection baseline.
    "ksadk/runtime/runner_adapter.py": 1220,
    "ksadk/sessions/local_service.py": 1080,
    "ksadk/sessions/postgres_service.py": 1019,
    # Agent Runtime v2 Phase 2 reviewed integration baseline. The 0.8.3
    # release deliberately freezes these exact post-integration sizes rather
    # than silently raising the global limit. Any additional line in one of
    # these modules turns the guard red again; responsibility extraction is a
    # separately scheduled maintenance item after the release.
    "ksadk/plugins/bridges/dsh.py": 1116,
    "ksadk/plugins/ecosystem_bridge.py": 1105,
    "ksadk/studio/api.py": 2038,
    "ksadk/studio/authoring_coordinator.py": 1027,
    "ksadk/studio/cloud.py": 2099,
    "ksadk/studio/otel_trace.py": 1060,
    "ksadk/studio/resource_catalog.py": 1265,
    "ksadk/studio/run_service.py": 1764,
    "ksadk/studio/service.py": 2839,
    "ksadk/studio/shared_web.py": 1143,
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
        if line_count > _FAIL_LINES and (legacy_limit is None or line_count > legacy_limit):
            oversized.append(f"{relative}: {line_count}")
        elif line_count > _WARN_LINES:
            large.append(f"{relative}: {line_count}")
    if large:
        warnings.warn(
            "Python 模块已超过 700 行，请在继续扩张前说明保留理由或拆分：\n" + "\n".join(large),
            stacklevel=1,
        )
    assert not oversized, "以下生产 Python 模块超过 1000 行，必须先按职责拆分：\n" + "\n".join(
        oversized
    )

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
    "ksadk/api/client.py": 2801,
    # Current master already contains the 2,287-line builder integration; PCM
    # adds only its launch-context projection seam. Keep any further growth red.
    "ksadk/builders/code_builder.py": 2424,
    "ksadk/cli/cmd_create.py": 2084,
    "ksadk/cli/cmd_files.py": 1270,
    "ksadk/cli/cmd_hermes.py": 1441,
    # 0.8.5 kspmas native Responses hardening added the four-variant reasoning
    # delta handling (reasoning.delta/reasoning_text/reasoning_summary/summary_text)
    # on top of the prior 1,565 baseline.
    "ksadk/cli/cmd_invoke.py": 1578,
    "ksadk/cli/cmd_mcp.py": 1213,
    "ksadk/cli/cmd_openclaw.py": 4218,
    "ksadk/codex/client.py": 1521,
    "ksadk/configs/env_registry.py": 1125,
    "ksadk/conversations/message_projection.py": 1050,
    "ksadk/codex/runtime.py": 1084,
    "ksadk/deployment/providers/serverless.py": 1358,
    # Agent Runtime v2 Kernel delivery baseline. These files contain the
    # frozen contract/store implementations; any post-0.8.2 growth stays red
    # until the corresponding responsibility is extracted.
    "ksadk/kernel/bootstrap.py": 1092,
    "ksadk/kernel/execution_host_ingress.py": 1071,
    "ksadk/kernel/ingress.py": 1161,
    "ksadk/kernel/memory_store.py": 1147,
    "ksadk/kernel/postgres_store.py": 1787,
    "ksadk/kernel/sqlite_store.py": 1717,
    "ksadk/kernel/worker.py": 1060,
    "ksadk/runners/adk_runner.py": 2340,
    # 0.8.1 approval continuation baseline; split the LangGraph execution
    # paths at the next runner-focused maintenance pass.
    "ksadk/runners/langgraph_runner.py": 1415,
    # 0.8.1 canonical ToolGateway approval projection baseline.
    "ksadk/runtime/runner_adapter.py": 1220,
    "ksadk/sessions/local_service.py": 1080,
    "ksadk/server/routes/projection.py": 1103,
    "ksadk/sessions/postgres_service.py": 1287,
    # Agent Runtime v2 Release reviewed integration baseline. The 0.8.3
    # release deliberately freezes these exact post-integration sizes rather
    # than silently raising the global limit. Any additional line in one of
    # these modules turns the guard red again; responsibility extraction is a
    # separately scheduled maintenance item after the release.
    "ksadk/plugins/bridges/dsh.py": 1311,
    # Complete DSH Core installation and process supervision baselines. The
    # browser mini-runtime was removed; keep further growth red until these
    # orchestration seams are split into their own maintenance change.
    "ksadk/plugins/dsh_toolchain.py": 1106,
    "ksadk/plugins/providers/dsh_capabilities.py": 1450,
    "ksadk/plugins/teams/domain.py": 1501,
    "ksadk/plugins/ecosystem_bridge.py": 1105,
    "ksadk/studio/api.py": 2757,
    "ksadk/studio/authoring_coordinator.py": 1034,
    "ksadk/studio/cloud.py": 2361,
    "ksadk/studio/contracts.py": 1028,
    "ksadk/studio/dsh_capability_service.py": 1374,
    "ksadk/studio/dsh_provider_registration.py": 1114,
    "ksadk/studio/otel_trace.py": 1060,
    "ksadk/studio/resource_catalog.py": 1412,
    "ksadk/studio/run_service.py": 2477,
    "ksadk/studio/service.py": 3580,
    "ksadk/studio/shared_web.py": 1642,
    # Studio 重构(本地草稿/outbox/云端接入)+ 社区合并 + harness 长任务稳定化
    # 带来的合法增长;model_client/plugin_runtime 为新增超千行模块,后续拆分。
    "ksadk/studio/model_client.py": 1743,
    "ksadk/studio/plugin_runtime.py": 1044,
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

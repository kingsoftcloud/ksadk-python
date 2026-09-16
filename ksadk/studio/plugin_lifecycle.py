"""Credential-free failures shared by local plugin lifecycle surfaces."""

from __future__ import annotations

import re
from typing import Any

from ksadk.studio.errors import StudioError

RECOVERY_URL = "/studio-recovery/"
_MESSAGES = {
    "DSH_PROFILE_IN_USE": "仍有执行或审批正在进行，请结束执行后再变更插件 Profile。",
    "authority_in_use": "已有本地宿主占用团队存储；请等待原宿主退出后重试。",
    "artifact_migration_required": (
        "插件版本已更新，需要先检查旧团队数据。已结束的兼容历史可以备份升级。"
    ),
    "migration_active_runs": "仍有在途任务，请先恢复原版本并核对执行状态，再升级历史。",
    "local_upgrade_unsupported": "此历史版本需要专门迁移，请保留备份并使用原锁定版本。",
    "migration_uncertain_execution": "仍有结果未确认的执行，请先核查原单再迁移。",
    "execution_node_in_use": "这个工作区已有执行节点在运行，请等待其退出后重试。",
    "DSH_TOOLCHAIN_MISSING": "本地插件工具链缺失，请先安装或修复 DSH。",
    "dsh_toolchain_required": "本地插件工具链尚未配置，请先安装并启用 DSH。",
    "teams_credentials_required": "团队服务连接凭据不可用，请更新本地配置后重试。",
    "COMPANION_GRAPH_INCOMPLETE": "团队插件组件不完整，请修复或禁用插件。",
    "COMPANION_GRAPH_NOT_READY": "团队插件组件尚未就绪，请重试或禁用插件。",
}
_REQUIRES_CHANGE = {
    "artifact_migration_required",
    "artifact_required",
    "artifact_scope_mismatch",
    "COMPANION_GRAPH_INCOMPLETE",
    "COMPANION_PROFILE_MISMATCH",
    "DSH_HOME_VERSION_UNVERIFIED",
    "DSH_TOOLCHAIN_MISSING",
    "authority_profile_unavailable",
    "dsh_toolchain_required",
}


def lifecycle_failure(error: BaseException, stage: str) -> dict[str, Any]:
    """Never expose exception text, transport addresses, or credentials."""
    code = getattr(error, "code", "plugin_lifecycle_failed")
    if not isinstance(code, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,96}", code):
        code = "plugin_lifecycle_failed"
    details = getattr(error, "details", {})
    prior_stage = details.get("stage") if isinstance(details, dict) else None
    if isinstance(prior_stage, str) and re.fullmatch(r"[a-z_]{1,64}", prior_stage):
        stage = prior_stage
    reason = details.get("reason") if isinstance(details, dict) else None
    if not isinstance(reason, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,96}", reason):
        reason = None
    return {
        "code": code,
        "message": _MESSAGES.get(code, "插件暂时不可用，请查看诊断信息或重试。"),
        "stage": stage,
        "retryable": (reason or code) not in _REQUIRES_CHANGE,
        "recoveryUrl": RECOVERY_URL,
        **({"reason": reason} if reason else {}),
    }


def lifecycle_error(error: BaseException, stage: str) -> StudioError:
    failure = lifecycle_failure(error, stage)
    if isinstance(error, StudioError):
        return StudioError(
            error.code,
            error.message,
            status_code=error.status_code,
            field=error.field,
            details={**error.details, **failure},
        )
    status = getattr(error, "status", 503)
    if not isinstance(status, int) or not 400 <= status <= 599:
        status = 503
    return StudioError(
        failure["code"],
        _MESSAGES.get(failure["code"], "插件未能完成装配，请打开恢复页面查看状态。"),
        status_code=status,
        details=failure,
    )

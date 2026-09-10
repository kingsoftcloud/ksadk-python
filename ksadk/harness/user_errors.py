"""用户可读错误消息翻译（plan §7.2 / 体验缺口 B）。

把引擎层的技术错误分类翻译为用户可读的中文消息，
不暴露 raw exception / stack trace / 内部状态。

分层：
  - 引擎层：classify_model_failure → 稳定类别（已有）
  - 本层：类别 → 用户可读消息
  - 事件层：RUN_FAILED payload.error 使用本层消息

不改变引擎层错误分类逻辑，只加一层用户面向的翻译。
"""

from __future__ import annotations

from ksadk.harness.model_provider import ClassifiedModelFailure, ModelFailureKind

#: 错误类别 → 用户可读消息模板。
_USER_MESSAGES: dict[ModelFailureKind, str] = {
    ModelFailureKind.RATE_LIMIT: "模型调用频率受限，请稍后重试",
    ModelFailureKind.TIMEOUT: "模型响应超时，请检查网络或重试",
    ModelFailureKind.UNAVAILABLE: "模型服务暂时不可用，请稍后重试",
    ModelFailureKind.AUTHENTICATION: "模型凭证无效，请检查 API Key 配置",
    ModelFailureKind.PERMISSION: "模型访问被拒绝，请检查权限配置",
    ModelFailureKind.NOT_FOUND: "指定的模型不存在，请检查模型配置",
    ModelFailureKind.CONTEXT_LENGTH: "对话上下文过长，正在自动压缩后重试",
    ModelFailureKind.INVALID_REQUEST: "模型请求格式异常，请重试或调整提示",
    ModelFailureKind.TRANSPORT: "模型网络连接异常，正在自动重试",
    ModelFailureKind.UNKNOWN: "Agent 运行遇到错误，请重试",
}

#: 工具错误通用消息（不暴露 raw exception）。
_TOOL_ERROR_MESSAGE = "工具调用失败，请检查工具配置或稍后重试"
_APPROVAL_DENIED_MESSAGE = "工具调用被审批拒绝，未执行"


def user_facing_error(
    error: Exception | None = None,
    *,
    failure: ClassifiedModelFailure | None = None,
    context: str = "",
) -> str:
    """返回用户可读的错误消息。

    Args:
        error: 原始异常（用于分类，不暴露给用户）。
        failure: 已分类的失败（优先于 error）。
        context: 上下文标识（"model" / "tool" / "approval"），决定用哪套消息。

    Returns:
        用户可读的中文消息，不含 raw exception / stack trace。
    """
    if context == "tool":
        return _TOOL_ERROR_MESSAGE
    if context == "approval":
        return _APPROVAL_DENIED_MESSAGE

    if failure is None and error is not None:
        from ksadk.harness.model_provider import classify_model_failure

        failure = classify_model_failure(error)

    if failure is not None:
        return _USER_MESSAGES.get(failure.kind, "Agent 运行遇到错误，请重试")

    return "Agent 运行遇到错误，请重试"


__all__ = ["user_facing_error"]

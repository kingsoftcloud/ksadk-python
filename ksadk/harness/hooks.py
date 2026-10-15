# ksadk/harness/hooks.py
"""G5 Provider 级扩展拦截面：四钩子协议与执行器（spec §5.2）。

纪律：
- 钩子按注册顺序同步 await；单钩子异常或协议违约（链式方法返回非预期类型）
  → 注入的 event_fn 发 ``policy.decision``（四钩子一律可见降级，无开关；
  payload 满足 POLICY_DECISION 必填键契约：action="hook_failed"、reason=摘要，
  并附 outcome/hook_index/method/error 诊断键），该钩子输出降级丢弃、链式主值
  回退到上一有效值，其余钩子独立执行——可见降级，不静默吞。event_fn 上报路径
  自身失败亦被吞（stderr 记录），不得穿出 run_hooks 中止钩子链。
- 钩子不获得 approval 绕过权、不获得沙箱豁免；on_provider_request 返回中
  model 键的修改由调用方（引擎）忽略。
- payload 键契约：on_context → {"messages": list[dict]}；
  on_provider_request → {"model": str, "messages": list[dict]}；
  on_provider_response → {"model": str, "usage": dict}（只读）；
  on_tool_result → {"call": {call_id,name,arguments}, "result": {content: str}}。
- 已知限制（provider 钩子的执行边界）：压缩/规划期模型调用不经
  on_provider_request/on_provider_response（prepare_context 在建图前、
  _before_model 设置活跃 Run 之前发生，钩子按「无可归属 Run」跳过 + stderr）；
  recover_model_overflow 路径经钩子（发生在 reason 节点内部）。降级事件先入
  引擎缓冲、由模型调用节点统一 flush，避免 mid-call engine._event 破坏
  run.seq 与 loop seq_start 本地计数的对齐（详见 hooked_reasoner 模块 docstring）。
- on_context 的降级不经引擎缓冲：prepare_context / recover_model_overflow 的
  钩子调用发生在模型调用回合之外（预图调用或 reason 节点内压缩路径），
  policy.decision 直接经管线 _hook_event 追加 run.events（seq 由引擎 _event
  推进，不与 loop 本地计数冲突），是 seq 安全的特例。
- 并行工具批次内，多个 tool call 各自的 on_tool_result 钩子链按链内注册顺序
  交错执行（按批次并发调度），不保证全局串行；钩子实现不得依赖跨 call 的
  全局执行顺序。
"""

from __future__ import annotations

import asyncio
import sys
from copy import deepcopy
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Protocol

from ksadk.harness.events import EventType


@dataclass(frozen=True)
class RunContext:
    run_id: str
    agent_id: str
    user_id: str
    session_id: str


class HookType(str, Enum):
    """G26 类型分档（§8 反向清单：不含 agent——hook 内起 agent拒绝）。"""

    COMMAND = "command"   # 执行外部命令
    HTTP = "http"         # HTTP 调用
    PROMPT = "prompt"     # 注入/改写 prompt


@dataclass(frozen=True)
class HookDeclaration:
    """G26 钩子声明：钩子对象 + 类型 + blocking 标记。

    裁决2：所有类型默认 non_blocking；blocking 只由显式 ``blocking=True`` 决定
    （默认不阻断主循环，对齐 §22「崩溃/超时一律降级为可见 non_blocking_error」）。
    """

    hook: "ProviderHook"
    type: HookType = HookType.PROMPT
    blocking: bool = False


def _as_declaration(item: Any) -> HookDeclaration:
    """向后兼容：裸 ProviderHook 自动包成默认 HookDeclaration。"""
    if isinstance(item, HookDeclaration):
        return item
    return HookDeclaration(hook=item)


class ProviderHook(Protocol):
    async def on_context(
        self, run: RunContext, messages: list[dict[str, Any]]
    ) -> list[dict[str, Any]]: ...

    async def on_provider_request(
        self, run: RunContext, request: dict[str, Any]
    ) -> dict[str, Any]: ...

    async def on_provider_response(
        self, run: RunContext, response_meta: dict[str, Any]
    ) -> None: ...

    async def on_tool_result(
        self, run: RunContext, call: dict[str, Any], result: dict[str, Any]
    ) -> dict[str, Any]: ...


async def run_hooks(
    hooks: tuple[Any, ...],
    method_name: str,
    run: RunContext,
    event_fn: Callable[..., Any],
    *,
    payload: dict[str, Any],
) -> Any:
    """按注册顺序执行钩子；单钩子异常可见降级（spec §5.2）。

    四钩子一律可见降级（无 degradable 开关）：单钩子异常或协议违约（链式方法
    返回非预期类型）都发 ``policy.decision(action="hook_failed")`` 后钩子链继续
    （违约输出丢弃，沿用上一有效值——与调用侧末态守卫同语义）。
    on_provider_response 返回 None（只读观测）；其余方法返回链式处理的
    payload 主值（on_context/on_tool_result → 列表/dict，
    on_provider_request → dict）。method_name 非四钩子之一抛 ValueError。
    """
    if method_name not in (
        "on_context",
        "on_provider_request",
        "on_provider_response",
        "on_tool_result",
    ):
        raise ValueError(f"unknown hook method: {method_name!r}")
    # 链式方法的期望返回类型（on_provider_response 只读、不参与链式传递）。
    chained_types: dict[str, type] = {
        "on_context": list,
        "on_tool_result": dict,
        "on_provider_request": dict,
    }
    value: Any = None
    if method_name == "on_context":
        value = deepcopy(list(payload["messages"]))
    elif method_name == "on_tool_result":
        value = deepcopy(dict(payload["result"]))
    elif method_name == "on_provider_request":
        value = deepcopy(dict(payload))
    declarations = [_as_declaration(h) for h in hooks]
    for index, decl in enumerate(declarations):
        hook = decl.hook
        method = getattr(hook, method_name, None)
        if method is None:
            continue
        # ProviderHook 是 Protocol：其桩方法（body 为 ...）会被子类继承，使
        # getattr 永不为 None。只实现部分钩子的观察者（例如仅
        # on_provider_response）若被当成本方法调用会返回 None，从而把链式主值
        # 清空（脱敏/改写被静默丢弃）。未覆写协议的桩方法必须跳过。
        if getattr(method, "__func__", None) is getattr(ProviderHook, method_name, None):
            continue
        try:
            # A hook receives a detached working value. Only a valid return
            # is committed to the chain; exceptions/protocol failures cannot
            # corrupt the preceding value through nested mutable aliases.
            if method_name == "on_context":
                candidate = await method(run, deepcopy(list(value or [])))
            elif method_name == "on_tool_result":
                candidate = await method(
                    run, deepcopy(dict(payload["call"])), deepcopy(dict(value or {}))
                )
            elif method_name == "on_provider_request":
                candidate = await method(run, deepcopy(dict(value or {})))
            else:  # on_provider_response：只读观测
                await method(run, deepcopy(dict(payload)))
                continue
            if isinstance(candidate, chained_types[method_name]):
                # A successful hook may retain its returned object. Do not let
                # a later/background mutation rewrite the accepted chain.
                candidate = deepcopy(candidate)
        except asyncio.CancelledError:
            # G26 四态：被 Run 取消 → cancelled（不吞取消信号，原样 raise，仅记降级）
            _report_degradation(
                event_fn,
                run,
                method_name=method_name,
                hook_index=index,
                reason=f"{method_name}[{index}]: cancelled",
                error="hook execution cancelled",
                hook_outcome="cancelled",
                hook_type=decl.type.value,
            )
            raise
        except Exception as exc:  # noqa: BLE001 - 降级可见，不中断
            outcome = "blocking" if decl.blocking else "non_blocking_error"
            _report_degradation(
                event_fn,
                run,
                method_name=method_name,
                hook_index=index,
                reason=f"{method_name}[{index}]: {str(exc)[:200]}",
                error=str(exc)[:256],
                hook_outcome=outcome,
                hook_type=decl.type.value,
            )
            continue
        expected = chained_types[method_name]
        if not isinstance(candidate, expected):
            # 协议违约（链式方法返回非预期类型）：若继续向下一钩子传递，字符串
            # 会被 list()/dict() 按字符/键拆开，污染后续钩子的输入。丢弃该钩子
            # 输出、沿用上一有效值后继续——与 on_context 末态守卫同语义，可见
            # 降级不静默。协议违约一律 non_blocking_error（不阻断）。
            code = "non-list" if expected is list else "non-dict"
            _report_degradation(
                event_fn,
                run,
                method_name=method_name,
                hook_index=index,
                reason=(
                    f"{method_name}[{index}]: protocol violation ({code}); "
                    "previous chain value kept"
                ),
                error=f"{method_name} 返回 {type(candidate).__name__}，期望 {expected.__name__}",
                hook_outcome="non_blocking_error",
                hook_type=decl.type.value,
            )
            continue
        value = candidate
    return value


def _report_degradation(
    event_fn: Callable[..., Any],
    run: RunContext,
    *,
    method_name: str,
    hook_index: int,
    reason: str,
    error: str,
    hook_outcome: str = "non_blocking_error",
    hook_type: str = "prompt",
) -> None:
    """发可见降级事件；上报路径自身失败被吞（stderr），不穿出 run_hooks。

    G26 四态：``hook_outcome`` ∈ {blocking, non_blocking_error, cancelled}
    （success 不发事件）。``hook_type`` 记分档（command/http/prompt）。
    """
    try:
        event_fn(
            run,
            EventType.POLICY_DECISION,
            {
                # POLICY_DECISION 必填键契约（events.py）
                "action": "hook_failed",
                "reason": reason,
                # 诊断附加键
                "outcome": "hook_failed",
                "hook_outcome": hook_outcome,
                "hook_type": hook_type,
                "hook_index": hook_index,
                "method": method_name,
                "error": error,
            },
        )
    except Exception as report_exc:  # noqa: BLE001 - 上报失败不穿出
        print(
            f"[ksadk.harness.hooks] event_fn failed while reporting "
            f"hook degradation ({method_name}[{hook_index}]): "
            f"{report_exc!r}",
            file=sys.stderr,
        )


__all__ = ["ProviderHook", "RunContext", "run_hooks", "HookType", "HookDeclaration"]

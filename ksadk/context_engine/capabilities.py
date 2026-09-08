"""Runner Context Capabilities —— Prompt/Context/Memory 的 ownership 合同。

能力声明是可执行合同，不是展示标签：Runtime 按 ``ContextCapabilities`` 决定是否
编译/投影 Prompt、是否注入 History/Memory、是否执行 compaction。

本模块只落地数据模型与已知 Runner 的显式默认值；任何行为型接入（实际改写 Runner 输入、
按 capability 切换 ambient 注入、双阈值等）都在后续 PR，第一个 PR 仅做声明与 shadow 观测，
不改线上行为。未知自定义 Runner 默认采用最保守的 ``framework_assisted + opaque``。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

DeploymentMode = Literal["local", "ksadk_managed_cloud", "external_managed"]
"""部署位置（方案 §4.3 / §6.1）。

与 Context ownership 正交：描述实例在哪里运行、谁负责构建/扩缩容/运维，不描述谁拥有最终
模型输入。``local`` / ``ksadk_managed_cloud`` / ``external_managed``。不得据字符串判断
Context owner（``ksadk_managed_cloud`` 不自动等于 ``ksadk_owned``）。
"""

ContextIntegrationMode = Literal["ksadk_hosted", "framework_assisted", "native_runtime"]
"""KsADK 对最终模型输入的控制程度。

- ``ksadk_hosted``: KsADK 负责编译 Prompt、候选选择、预算、compaction 和最终输入组装。
- ``framework_assisted``: KsADK 提供统一 CompiledPrompt/Policy/Memory/观测，框架负责
  投影到原生 instruction/state/store。
- ``native_runtime``: KsADK 只传递版本化 instructions、平台边界和外部 Memory hook，
  原生 Runtime 持有 Agent loop/history/compaction/最终输入。
"""

ContextOwner = Literal["ksadk", "framework", "native"]
"""某一关注点（prompt/history/compaction/memory/skill）的实际所有者。"""

ContextAccuracy = Literal["exact", "runtime_reported", "estimated", "opaque"]
"""Context 观测精度等级（方案 6.3）。

- ``exact``: KsADK 生成最终模型输入并用匹配 tokenizer 计算。
- ``runtime_reported``: 原生 Runtime/模型返回了实际 usage 或 context 统计。
- ``estimated``: KsADK 只能对提交给 Runner 的内容做启发式估算。
- ``opaque``: Runner 不暴露最终输入或可靠 usage，只记录来源/hash/能力缺口。
"""


@dataclass(frozen=True)
class ContextCapabilities:
    """单个 Runner 的 Context 接入能力与 ownership 声明。

    必须由 Runner 实现或由 KsADK 为已知 Runner 提供显式默认值，不能仅靠 ``hasattr``
    猜测。若实际 usage 或事件证明声明不一致，应记录 ``context.capability_mismatch``
    并停止对该 Runner 启用行为型 Context Engine（该熔断逻辑留后续 PR）。
    """

    integration_mode: ContextIntegrationMode
    prompt_owner: ContextOwner
    history_owner: ContextOwner
    compaction_owner: ContextOwner
    memory_owner: ContextOwner
    skill_owner: ContextOwner
    # Runner 投影 Prompt 时实际使用的目标 SDK 承载形式，例如
    # ``{"system_message","state"}`` / ``{"instruction","session","memory_service"}`` /
    # ``{"base_instructions","thread"}``。空集表示未知/不投影。
    prompt_projection: frozenset[str]
    memory_read: bool
    memory_write: bool
    core_memory: bool
    native_skills: bool
    token_accounting: ContextAccuracy
    supports_context_snapshot: bool


def DEFAULT_CONTEXT_CAPABILITIES() -> ContextCapabilities:
    """未知自定义 Runner 的保守合同：framework_assisted + opaque，不启用任何行为型接入。"""
    return ContextCapabilities(
        integration_mode="framework_assisted",
        prompt_owner="framework",
        history_owner="framework",
        compaction_owner="framework",
        memory_owner="framework",
        skill_owner="framework",
        prompt_projection=frozenset(),
        memory_read=False,
        memory_write=False,
        core_memory=False,
        native_skills=False,
        token_accounting="opaque",
        supports_context_snapshot=False,
    )


def adk_context_capabilities() -> ContextCapabilities:
    """Google ADK：framework_assisted。

    instructions 拼进 new_message 文本 + agent.instruction 加载时改写；history 由 ADK
    SessionService 拥有（忽略 payload.history）；STM/LTM 作为 memory_service 注入 +
    load/save_memory 工具；skills 完整注入（manifest 仅 name/desc/version）；无 compaction。
    """
    return ContextCapabilities(
        integration_mode="framework_assisted",
        prompt_owner="framework",
        history_owner="framework",
        compaction_owner="framework",
        memory_owner="framework",
        skill_owner="framework",
        prompt_projection=frozenset({"instruction", "session", "memory_service"}),
        memory_read=True,
        memory_write=True,
        core_memory=False,
        native_skills=True,
        token_accounting="runtime_reported",
        supports_context_snapshot=True,
    )


def langgraph_context_capabilities() -> ContextCapabilities:
    """LangGraph：framework_assisted，KsADK 侧参与 prompt/history/compaction 投影。

    instructions→SystemMessage（或 ``ksadk_prepare_state`` hook）；history 由 runner
    组装（history dict→HumanMessage/AIMessage）；memory=checkpointer + memory_context
    payload 字段；无 skills；无 compaction。
    """
    return ContextCapabilities(
        integration_mode="framework_assisted",
        prompt_owner="ksadk",
        history_owner="ksadk",
        compaction_owner="ksadk",
        memory_owner="framework",
        skill_owner="framework",
        prompt_projection=frozenset({"system_message", "state"}),
        memory_read=True,
        memory_write=False,
        core_memory=False,
        native_skills=False,
        token_accounting="estimated",
        supports_context_snapshot=True,
    )


def langchain_context_capabilities() -> ContextCapabilities:
    """LangChain：framework_assisted，继承 LangGraph 的 prompt 投影但 history/compaction 交框架。"""
    return ContextCapabilities(
        integration_mode="framework_assisted",
        prompt_owner="ksadk",
        history_owner="framework",
        compaction_owner="framework",
        memory_owner="framework",
        skill_owner="framework",
        prompt_projection=frozenset({"system_message", "state"}),
        memory_read=False,
        memory_write=False,
        core_memory=False,
        native_skills=False,
        token_accounting="estimated",
        supports_context_snapshot=False,
    )


def deepagents_context_capabilities() -> ContextCapabilities:
    """DeepAgents：framework_assisted，LangGraph 系编译图，history/compaction 交框架。"""
    return ContextCapabilities(
        integration_mode="framework_assisted",
        prompt_owner="ksadk",
        history_owner="framework",
        compaction_owner="framework",
        memory_owner="framework",
        skill_owner="framework",
        prompt_projection=frozenset({"system_message", "state"}),
        memory_read=False,
        memory_write=False,
        core_memory=False,
        native_skills=False,
        token_accounting="estimated",
        supports_context_snapshot=False,
    )


def codex_context_capabilities() -> ContextCapabilities:
    """Codex：native_runtime。

    base_instructions 移交后端 thread；history 由后端 thread_id 拥有；无 memory hook；
    无 skills 暴露；compaction 后端拥有。KsADK 不重复注入完整 Transcript、不运行第二套
    compaction。
    """
    return ContextCapabilities(
        integration_mode="native_runtime",
        prompt_owner="native",
        history_owner="native",
        compaction_owner="native",
        memory_owner="native",
        skill_owner="native",
        prompt_projection=frozenset({"base_instructions", "thread"}),
        memory_read=False,
        memory_write=False,
        core_memory=False,
        native_skills=True,
        token_accounting="runtime_reported",
        supports_context_snapshot=True,
    )


# detection_result.type.value → 已知 Runner capability 工厂。显式枚举，不靠 hasattr。
_KNOWN_RUNNER_CAPABILITIES: dict[str, Any] = {
    "adk": adk_context_capabilities,
    "langgraph": langgraph_context_capabilities,
    "langchain": langchain_context_capabilities,
    "deepagents": deepagents_context_capabilities,
    "codex": codex_context_capabilities,
}


def _runner_type_value(runner: Any) -> str:
    """读取 runner.detection_result.type.value，兼容缺失字段。返回小写字符串。"""
    detection_result = getattr(runner, "detection_result", None)
    if detection_result is None:
        return ""
    detection_type = getattr(detection_result, "type", None)
    if detection_type is None:
        return ""
    value = getattr(detection_type, "value", detection_type)
    return str(value or "").strip().lower()


def _capabilities_for_detection_type(value: str) -> ContextCapabilities:
    """按 detection_result.type.value 显式分派已知 Runner capability，未知走 DEFAULT。

    纯 registry 查找，不调用 runner 的 ``describe_context_capabilities``，因此无递归风险：
    ``BaseRunner.describe_context_capabilities`` 默认实现直接走本函数。
    """
    factory = _KNOWN_RUNNER_CAPABILITIES.get(value)
    if factory is not None:
        return factory()
    return DEFAULT_CONTEXT_CAPABILITIES()


def capabilities_for_runtime_type(runtime_type: str | None) -> ContextCapabilities:
    """按 ``runtime_type``（平台边界 ``BaseRuntime.runtime_type``）显式分派 capability。

    对 framework runner，``runtime_type`` 与 ``detection_result.type.value`` 一致
    （adk/langgraph/langchain/deepagents/codex），故 canonical conversation execution
    路径在 ``build_run_input`` 阶段（尚未拿到 adapter/runner 实例）也能取得正确 ownership，
    不落成默认 opaque。未知 runtime_type 走 DEFAULT。
    """
    normalized = str(runtime_type or "").strip().lower()
    return _capabilities_for_detection_type(normalized)


_CAPABILITY_HASH_FIELDS: tuple[str, ...] = (
    "integration_mode",
    "prompt_owner",
    "history_owner",
    "compaction_owner",
    "memory_owner",
    "skill_owner",
    "memory_read",
    "memory_write",
    "core_memory",
    "native_skills",
    "token_accounting",
    "supports_context_snapshot",
)


def capability_hash(caps: ContextCapabilities) -> str:
    """对 capability 稳定字段做 SHA-256，供 Plan/Trace 记录 ``capability_hash``。

    ``prompt_projection`` 是 frozenset，按排序后元素拼接以保证确定性。不含 ``metadata``。
    """
    import hashlib
    import json

    payload = {field: getattr(caps, field) for field in _CAPABILITY_HASH_FIELDS}
    projection = sorted(getattr(caps, "prompt_projection", frozenset()) or [])
    payload["prompt_projection"] = projection
    serialized = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return "sha256:" + hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def capabilities_for_runner(runner: Any | None) -> ContextCapabilities:
    """统一 lookup：优先 runner 自身的 ``describe_context_capabilities()``，否则按 detection
    type 显式分派，未知走 DEFAULT。

    不依赖 ``hasattr`` 猜测 ownership（方案 6.1）。``BaseRunner`` 的默认
    ``describe_context_capabilities``
    走 ``_capabilities_for_detection_type``，故本函数对 BaseRunner 子类不会递归。已被 compaction
    门控（``runtime_preparation`` proactive compaction）与 shadow plan / conformance 测试消费。
    """
    if runner is None:
        return DEFAULT_CONTEXT_CAPABILITIES()

    describe = getattr(runner, "describe_context_capabilities", None)
    if callable(describe):
        try:
            caps = describe()
        except Exception:
            caps = None
        if isinstance(caps, ContextCapabilities):
            return caps

    return _capabilities_for_detection_type(_runner_type_value(runner))


# ---- Capability Mismatch 检测与熔断（方案 §6.1 / §8.3）----

# 进程内 best-effort 熔断记录：runner 标识 → 已熔断。只影响"是否对该 Runner 启用行为型
# Context Engine"，不影响 shadow 观测与正常执行（方案 §6.1）。pod 重启清空。
_MISMATCH_CIRCUIT: dict[str, bool] = {}


def detect_capability_mismatch(
    *,
    declared: ContextCapabilities,
    actual_prompt_owner: str | None = None,
    actual_history_owner: str | None = None,
    actual_compaction_owner: str | None = None,
    runtime_reported_usage: bool | None = None,
    duplicate_history_injected: bool = False,
    double_compaction: bool = False,
) -> str | None:
    """检测声明的 capability 与运行时实际证据是否一致（方案 §6.1）。

    返回 mismatch 原因字符串（``prompt_owner``/``history_owner``/``compaction_owner``/
    ``token_accounting``/``duplicate_history``/``double_compaction``）；一致返回 ``None``。
    熔断由 ``mark_capability_mismatch`` / ``is_capability_circuit_open`` 表达。
    """
    reasons: list[str] = []
    if actual_prompt_owner is not None and actual_prompt_owner != declared.prompt_owner:
        reasons.append(f"prompt_owner:{declared.prompt_owner}!={actual_prompt_owner}")
    if actual_history_owner is not None and actual_history_owner != declared.history_owner:
        reasons.append(f"history_owner:{declared.history_owner}!={actual_history_owner}")
    if actual_compaction_owner is not None and actual_compaction_owner != declared.compaction_owner:
        reasons.append(f"compaction_owner:{declared.compaction_owner}!={actual_compaction_owner}")
    if runtime_reported_usage is False and declared.token_accounting == "runtime_reported":
        reasons.append("token_accounting:declared_runtime_reported_but_no_usage")
    if duplicate_history_injected:
        reasons.append("duplicate_history_injected")
    if double_compaction:
        reasons.append("double_compaction")
    return ";".join(reasons) if reasons else None


def _mismatch_key(runner: Any | None, runtime_type: str | None) -> str:
    rt = _runner_type_value(runner) if runner is not None else str(runtime_type or "")
    return rt or "unknown"


def mark_capability_mismatch(runner: Any | None = None, runtime_type: str | None = None) -> None:
    """标记某 Runner 触发 capability mismatch 熔断（方案 §6.1）。

    熔断后 ``is_capability_circuit_open`` 返回 True，行为型 Context Engine 对该 Runner 停用；
    shadow 观测与正常 Runner 执行不受影响。
    """
    _MISMATCH_CIRCUIT[_mismatch_key(runner, runtime_type)] = True


def is_capability_circuit_open(runner: Any | None = None, runtime_type: str | None = None) -> bool:
    """该 Runner 是否已因 capability mismatch 熔断（方案 §6.1）。"""
    return _MISMATCH_CIRCUIT.get(_mismatch_key(runner, runtime_type), False)


def reset_capability_circuit(runner: Any | None = None, runtime_type: str | None = None) -> None:
    """清除熔断标记（测试/运维用）。"""
    key = _mismatch_key(runner, runtime_type)
    _MISMATCH_CIRCUIT.pop(key, None)


# ---- Ownership 可选范围与校验（方案 §5.2：ownership 不允许任意选择）----

# 按 runtime_type 列出 Studio 可选 ownership（context.ownership 字段值）。
# auto = 由 capability 推导；ksadk/framework/native 必须与 capability 兼容。
_OWNERSHIP_CHOICES: dict[str, tuple[str, ...]] = {
    "codex": ("native",),
    "adk": ("framework",),  # 后续开放 assisted
    "langgraph": ("framework", "ksadk"),
    "langchain": ("framework",),
    "deepagents": ("framework",),
}


def allowed_ownership_choices(runtime_type: str | None) -> tuple[str, ...]:
    """该 runtime 在 Studio 中可选的 ownership（方案 §5.2）。未知 runtime 走保守 framework。"""
    key = str(runtime_type or "").strip().lower()
    return _OWNERSHIP_CHOICES.get(key, ("framework",))


def validate_ownership_for_runtime(ownership: str, *, runtime_type: str | None) -> None:
    """校验 ownership 与 runtime capability 兼容（方案 §5.2）。

    不支持组合时抛 ``ValueError``，Studio 据 it 返回 capability mismatch，不静默降级。
    ``auto`` 总是合法（运行时按 capability 推导）。
    """
    if ownership == "auto":
        return
    allowed = allowed_ownership_choices(runtime_type)
    if ownership not in allowed:
        raise ValueError(
            f"ownership={ownership!r} 不被 runtime={runtime_type!r} 支持；"
            f"可选: {list(allowed)}（方案 §5.2）"
        )


def resolve_ownership(ownership: str, *, runtime_type: str | None) -> str:
    """把 ``context.ownership`` 解析为实际 prompt ownership（ksadk/framework/native）。

    ``auto`` → 解析为该 runtime 的**保守产品默认**（方案 §5.2：langgraph/adk 默认 framework，
    codex 默认 native），而非 capability 上限——capability 表示“能接管”，不代表“默认接管”。
    显式值原样返回（已由 ``validate_ownership_for_runtime`` 校验）。
    """
    if ownership == "auto":
        rt = str(runtime_type or "").strip().lower()
        if rt == "codex":
            return "native"
        return "framework"  # langgraph/adk/langchain/deepagents 默认 framework
    return ownership


def assert_capability_not_circuit_open(
    *, runner: Any | None = None, runtime_type: str | None = None, label: str = ""
) -> None:
    """行为型 Context Engine 接入前的门禁（方案 §6.1）。

    若该 Runner 已因 capability mismatch 熔断，则抛 ``CapabilityCircuitOpen``——调用方据
    此回退 shadow/旧路径，**不**继续行为型接管。``label`` 仅用于错误信息，便于诊断是哪个接入点
    被熔断拦下。shadow 观测与正常 Runner 执行不受此门禁影响。
    """
    if is_capability_circuit_open(runner=runner, runtime_type=runtime_type):
        raise CapabilityCircuitOpen(
            runtime_type=_mismatch_key(runner, runtime_type),
            label=label or "behavioral_context_engine",
        )


class CapabilityCircuitOpen(RuntimeError):
    """Runner 因 capability mismatch 被熔断，行为型 Context Engine 对其停用（方案 §6.1）。"""

    def __init__(self, *, runtime_type: str, label: str) -> None:
        self.runtime_type = runtime_type
        self.label = label
        super().__init__(
            f"capability circuit open for runtime={runtime_type!r} at {label!r}; "
            "behavioral context engine disabled for this runner"
        )

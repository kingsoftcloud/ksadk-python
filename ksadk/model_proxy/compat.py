"""Provider 兼容配置：声明式数据 + 纯函数清洗（对齐 Claude Code / Codex / OpenClaw 的形态）。

设计纪律（来自本轮开源对照结论，见 docs/research/2026-09-18-claude-code-harness-learnings.md）：

- 模型与端点之间的差异**只允许声明在这一层**，不允许出现在"传输选择"或
  "响应解析"的分支里。历史上我们把 ``glm-5.3-flash`` 硬编码进传输判断、
  把"仅 reasoning 的回合"判成致命错误，两次都造成主路径不可用。
- 未知端点一律走 ``permissive``：默认放手，按证据（profile）收紧。
- 清洗是纯函数：端点不接受的字段**剥掉**，而不是让请求失败。
- 覆盖顺序：显式参数 > 环境变量 ``KSADK_MODEL_COMPAT_RULE`` > 按 base_url/模型名推断。
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass, replace
from typing import Any, Mapping

from .transform import ReasoningEffortPolicy, clamp_reasoning_effort, reasoning_effort_policy

#: reasoning_content 在往返（把历史 assistant 消息回传）时的处理策略。
REASONING_ECHO_MODES = ("always-preserve", "drop-on-non-thinking", "strip")


@dataclass(frozen=True)
class CompatProfile:
    """一个 OpenAI 兼容端点的能力声明。"""

    name: str
    #: 端点是否接受 chat completions 的 ``stream_options.include_usage``
    #: （严格端点会拒绝未知顶层键）。
    supports_stream_usage_option: bool = True
    #: 端点是否接受窗口顶层的 ``thinking`` 字段（GLM/Z.AI、DeepSeek 等）。
    supports_thinking_field: bool = True
    supports_reasoning_effort: bool = True
    # Private wire material needed by a subsequent model request. Never an
    # answer, summary, tool argument or public reasoning event.
    provider_replay_mode: str = "none"
    #: reasoning_content 往返策略；见 ``REASONING_ECHO_MODES``。
    reasoning_echo: str = "drop-on-non-thinking"
    #: 该端点的流式在长工具/子代理回合可能长时间不关闭；声明为数据，
    #: 由调用方决定是否优先走缓冲请求，而不是在代码里按模型名判断。
    prefer_buffered_transport: bool = False
    #: 增量传输的"空闲进度"超时（秒）：连续这么久没有**内容**chunk（正文/
    #: 思考/工具调用）就认为这次流卡住了。None = 用引擎的每请求超时。
    #: 语义是"无进度"而非"总时长"，所以上游正常吐字的慢回合不会被误杀；
    #: 只有 keepalive 而一直没有内容的流才算卡住。
    stream_idle_timeout_seconds: int | None = None
    #: 工具回合是否关闭思考。始终思考的端点（GLM/Z.AI）常把输出预算耗在
    #: reasoning_content 上，导致工具回合不产出内容或调用（OpenClaw 文档
    #: 记录了同一现象与 thinking=off 的处置）。
    disable_thinking_for_tools: bool = False
    #: 该端点是否默认开启思考（始终思考模型）。用于下面的预算不变量。
    thinks_by_default: bool = False
    #: 该端点的思考预算（token）。None 表示未知；已知时用于不变量：
    #: 输出预算必须**大于**思考预算（对齐 Claude Code 的
    #: "max_tokens > thinking.budget_tokens" 约束），否则思考会把预算吃光。
    thinking_budget_tokens: int | None = None
    #: slot 优化：该端点推荐的输出预算。请求低于此值时抬到该值，高于时不干预。
    #: 依据 Claude Code 的 CAPPED_DEFAULT_MAX_TOKENS=8000——过大的 max_tokens 会按
    #: slot 预留推理容量，导致排队/超时/重试（BQ p99 输出仅 4,911）。
    preferred_output_tokens: int | None = None
    #: 命中这些模型名片段时视为"思考型模型"（影响 reasoning_echo 判定）。
    thinking_model_hints: tuple[str, ...] = ()
    #: Controller-owned action turns may need a lower reasoning level than the
    #: provider default.  Keep the rule in endpoint/model capability data,
    #: rather than hiding model-name branches in Studio's transport code.
    #: Each pair is ``(normalized model-name prefix, effort)``.
    delegation_reasoning_effort_rules: tuple[tuple[str, str], ...] = ()
    #: Native summary calls have their own bounded text output contract.
    #: Only evidence-backed endpoint/model pairs may supply a default; an
    #: explicit run setting still wins. Do not lower ordinary task reasoning.
    compaction_reasoning_effort_rules: tuple[tuple[str, str], ...] = ()
    # Purpose-only switch for measured endpoint/model pairs. Field support
    # alone does not prove that disabling thinking is accepted by a model.
    compaction_thinking_disabled_models: tuple[str, ...] = ()
    #: Some compatible endpoints place private reasoning in ``content`` and
    #: delimit it with this closing marker, sometimes without an opening tag.
    #: Only an explicitly profiled endpoint may interpret this nonstandard wire.
    inline_reasoning_end_marker: str | None = None
    #: Explicit provider projection for a host-required submission. K2.7 Code
    #: only accepts auto/none while thinking is mandatory. The host's schema
    #: and rejection of plain text remain authoritative; this is not a retry
    #: fallback. None keeps existing standard required behavior and identity.
    required_tool_choice_projection: str | None = None
    note: str = ""
    # Resolved Native adapters pin both the identity and the actual mapping.
    reasoning_effort_policy: ReasoningEffortPolicy | None = None
    # None retains the declared endpoint policy; () means disabled is never
    # accepted. A finite set requires a known effective effort in that set.
    thinking_disabled_efforts: tuple[str, ...] | None = None
    # Exact catalog capability; None is unknown, disabled is an explicit opt-out.
    anthropic_block_binding: str | None = None
    # Ordered operator messages are distinct from turn-scoped/beta tool changes.
    # None is unknown; False overrides catalog support for a custom endpoint.
    anthropic_mid_system: bool | None = None

    def __post_init__(self) -> None:
        if self.anthropic_mid_system is not None and type(self.anthropic_mid_system) is not bool:
            raise ValueError("unsupported Anthropic mid-conversation system policy")
        if self.anthropic_block_binding not in {None, "disabled", "drop_block"}:
            raise ValueError("unsupported Anthropic block binding policy")
        if self.provider_replay_mode not in {"none", "reasoning_content"}:
            raise ValueError("unsupported provider replay mode")
        if self.required_tool_choice_projection not in {None, "auto"}:
            raise ValueError("unsupported required tool choice projection")
        if self.reasoning_echo not in REASONING_ECHO_MODES:
            raise ValueError(
                f"unknown reasoning_echo {self.reasoning_echo!r}; "
                f"expected one of {REASONING_ECHO_MODES}"
            )


COMPAT_PROFILES: Mapping[str, CompatProfile] = {
    # 默认：放手，最大化兼容性。
    "permissive": CompatProfile("permissive", note="未知端点默认档"),
    "moonshot": CompatProfile("moonshot", thinking_model_hints=("kimi",)),
    # 只接受标准字段的严格端点。
    "openai": CompatProfile(
        "openai",
        supports_stream_usage_option=False,
        supports_thinking_field=False,
        reasoning_echo="strip",
    ),
    "strict-openai": CompatProfile(
        "strict-openai",
        supports_stream_usage_option=False,
        supports_thinking_field=False,
        reasoning_echo="strip",
        note="拒绝未知顶层键的网关/端点（Cerebras、部分 Qwen 部署）",
    ),
    "cerebras": CompatProfile(
        "cerebras",
        supports_stream_usage_option=False,
        supports_thinking_field=False,
        reasoning_echo="strip",
    ),
    "groq": CompatProfile(
        "groq",
        supports_stream_usage_option=False,
        supports_thinking_field=False,
        reasoning_echo="strip",
    ),
    # DeepSeek's current thinking-mode protocol requires reasoning_content
    # on tool-bearing history, including empty and non-tool assistant turns.
    # Native must seal it as well as allowing the transport to echo it; an
    # ordinary no-tool request may include it and the provider ignores it.
    "deepseek": CompatProfile(
        "deepseek",
        reasoning_echo="always-preserve",
        provider_replay_mode="reasoning_content",
        thinks_by_default=True,
        thinking_model_hints=("deepseek",),
        note="DeepSeek 要求回传 reasoning_content，否则 400",
    ),
    # GLM / Z.AI：长工具回合的流可能不关闭，因此保留流式但
    # 配“空闲进度”超时；超时后交给统一 provider policy 重试/切换。
    "zai": CompatProfile(
        "zai",
        stream_idle_timeout_seconds=60,
        # Z.AI 官方支持 thinking:{type:disabled}（OpenClaw 文档记载）；
        # "网关拒绝该字段"是 kspmas 的特性，由 kspmas-gateway 档承担。
        supports_thinking_field=True,
        disable_thinking_for_tools=True,
        thinks_by_default=True,
        thinking_budget_tokens=32_000,  # provider 元数据 max_reasoning_tokens
        thinking_model_hints=("glm", "zai", "bigmodel"),
        delegation_reasoning_effort_rules=(("glm-5.3", "low"),),
        inline_reasoning_end_marker="</think>",
        note="Z.AI 官方：支持 thinking 字段；长工具回合建议关思考",
    ),
    # kspmas 网关（我们实际使用的端点）。2026-09-21 对 glm-5.3-flash
    # 重新做了带工具的真实探针：仅传 ``thinking:{type:disabled}`` 时
    # reasoning_tokens=0；再配 ``tool_choice=required`` 时 1.13s 返回标准
    # bash tool call。旧的 2026-09-20 结论把一次 reasoning-only 采样误判为
    # “网关静默忽略 thinking”，不能继续阻止工具回合关闭思考。
    "kspmas-gateway": CompatProfile(
        "kspmas-gateway",
        # 流式 transport 的 idle 只判断“有没有应用层 chunk”，与语义上的正文/
        # 工具产出分离；整轮运行预算由 Harness controller 单独控制。
        stream_idle_timeout_seconds=60,
        supports_thinking_field=True,
        disable_thinking_for_tools=True,
        thinks_by_default=True,
        thinking_model_hints=("glm", "deepseek"),
        # OpenClaw's GLM-5.3 adapter maps off/minimal/low to the provider's
        # lowest valid value, ``reasoning_effort=low``.  Our live KSPMAS probe
        # confirms that this value is accepted while ``none``/``minimal`` are
        # rejected.  Apply it only to the controller's delegate decision;
        # explicit per-run effort continues to win.
        delegation_reasoning_effort_rules=(("glm-5.3", "low"),),
        # 2026-10-07: same retained failed summary request, original 1024 cap,
        # 3 slots each: low 1/3 complete summaries, none 3/3 with zero reasoning.
        # Z.AI documents GLM-5.2 low -> high and none -> no thinking. Match only
        # the measured endpoint/model; explicit settings still take precedence.
        # The retained DeepSeek-v4-flash summary probe likewise completed
        # 3/3 with none at the unchanged 1024 output cap. This is scoped to
        # this endpoint and summary purpose, never a task reasoning default.
        compaction_reasoning_effort_rules=(
            ("glm-5.2", "none"), ("deepseek-v4-flash", "none"),
        ),
        # 2026-10-13: retained failed 8192-cap summary, streaming, no tools.
        # disabled returned public text in 3/3 (one stop, two length); the
        # original two responses spent the cap on private reasoning only.
        # GLM-5.3 rejects effort=none; do not reuse the GLM-5.2 mapping.
        compaction_thinking_disabled_models=("glm-5.3-flash",),
        inline_reasoning_end_marker="</think>",
        note=(
            "kspmas 网关：工具回合关闭思考；流式 idle 按任意应用层 chunk 续期，"
            "reasoning-only 失控由 Harness 的动作进展预算恢复"
        ),
    ),
    # 小米 MiMo：与 DeepSeek 类似的思考型端点。
    "mimo": CompatProfile(
        "mimo",
        reasoning_echo="always-preserve",
        thinks_by_default=True,
        thinking_model_hints=("mimo",),
    ),
}

# Narrow endpoint/model overlays. The original Studio cohort rejected the
# KSPMAS tool-turn thinking switch for K2.7 Code (HTTP 400). Moonshot's official
# protocol also requires no thinking/effort override and preserved reasoning.
# Keep the endpoint transport policy; do not apply a gateway's GLM defaults to
# every model or turn a model-name substring into a universal provider rule.
_MODEL_PROFILES = {
    (rule, model): replace(
        COMPAT_PROFILES[rule],
        supports_thinking_field=False,
        supports_reasoning_effort=False,
        disable_thinking_for_tools=False,
        thinks_by_default=True,
        reasoning_echo="always-preserve",
        thinking_model_hints=("kimi-k2.7-code",),
        provider_replay_mode="reasoning_content",
        inline_reasoning_end_marker=None,
        required_tool_choice_projection="auto",
        note="K2.7 Code: mandatory thinking, no switch/effort, private protocol replay",
    )
    for rule in ("kspmas-gateway", "moonshot")
    for model in ("kimi-k2.7-code", "kimi-k2.7-code-highspeed")
}

# Exact API identifiers and disabled value domains from the official model
# catalog / thinking table. Do not extrapolate aliases or future releases.
_THINKING_DISABLED_EFFORTS = {
    "claude-fable-5-1": (),
    "claude-opus-5-5": (),
    "claude-sonnet-5-5": (),
    "claude-haiku-5-5": ("low", "medium", "high"),
}

# Official preserved-thinking table, not a future-version/name-substring guess.
_ANTHROPIC_BLOCK_BINDING = {
    "claude-fable-5-1": "drop_block",
    "claude-opus-5-5": "drop_block",
    "claude-sonnet-5-5": "drop_block",
    "claude-haiku-5-5": "drop_block",
}

# Exact identifiers already declared in the official catalog. Additional aliases
# or future deployments need an explicit profile; do not infer from a version.
_ANTHROPIC_MID_SYSTEM = frozenset({
    "claude-fable-5-1", "claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5",
})

DEFAULT_COMPAT_RULE = "permissive"


def project_inline_reasoning(text: str, profile: CompatProfile) -> str:
    """Return only public content from a profiled inline-reasoning response.

    A closing marker can occur without an opening marker on the provider wire.
    Never send the prefix to the Agent transcript or its final verifier. Other
    providers retain their content byte for byte.
    """

    marker = profile.inline_reasoning_end_marker
    if marker is None:
        return text
    if marker in text:
        return text.rsplit(marker, 1)[1]
    if text.lstrip().startswith("<think>"):
        return ""
    return text


class InlineReasoningProjector:
    """Keep profiled streaming content private until its wire shape is known."""

    def __init__(self, profile: CompatProfile) -> None:
        self._marker = profile.inline_reasoning_end_marker
        self._pending = ""
        self._released = self._marker is None

    def push(self, text: str) -> str:
        if self._released:
            return text
        self._pending += text
        assert self._marker is not None
        if self._marker not in self._pending:
            return ""
        public = self._pending.rsplit(self._marker, 1)[1]
        self._pending = ""
        self._released = True
        return public

    def seal(self) -> str:
        if self._released:
            return ""
        public = "" if self._pending.lstrip().startswith("<think>") else self._pending
        self._pending = ""
        self._released = True
        return public


_RULE_BY_URL_HINT: tuple[tuple[str, str], ...] = (
    # 具体网关优先于上游厂商：同一模型经不同网关时能力可能不同。
    ("kspmas", "kspmas-gateway"),
    ("cerebras", "cerebras"),
    ("groq", "groq"),
    ("deepseek", "deepseek"),
    ("zai", "zai"),
    ("bigmodel", "zai"),
    ("glm", "zai"),
    ("moonshot", "moonshot"),
    ("api.kimi.com", "moonshot"),
    ("openai.com", "openai"),
)
_RULE_BY_MODEL_HINT: tuple[tuple[str, str], ...] = (
    ("deepseek", "deepseek"),
    ("glm", "zai"),
    ("zai", "zai"),
    ("bigmodel", "zai"),
    ("mimo", "mimo"),
    ("gpt-", "openai"),
)


def resolve_compat_rule(
    *,
    base_url: str = "",
    model: str = "",
    explicit: str | None = None,
) -> str:
    """解析生效的兼容档位：显式 > 环境变量 > base_url > 模型名 > permissive。"""

    if explicit:
        return explicit if explicit in COMPAT_PROFILES else DEFAULT_COMPAT_RULE
    env_rule = os.getenv("KSADK_MODEL_COMPAT_RULE", "").strip()
    if env_rule:
        return env_rule if env_rule in COMPAT_PROFILES else DEFAULT_COMPAT_RULE
    url = str(base_url or "").lower()
    for hint, rule in _RULE_BY_URL_HINT:
        if hint in url:
            return rule
    name = str(model or "").lower().replace("_", "-")
    for hint, rule in _RULE_BY_MODEL_HINT:
        if hint in name:
            return rule
    return DEFAULT_COMPAT_RULE


def compat_profile(
    rule: str | None = None,
    *,
    base_url: str = "",
    model: str = "",
) -> CompatProfile:
    """取兼容档位；``rule`` 为空时按 base_url/模型名推断。"""

    resolved = rule or resolve_compat_rule(base_url=base_url, model=model)
    profile = COMPAT_PROFILES.get(resolved, COMPAT_PROFILES[DEFAULT_COMPAT_RULE])
    name = str(model or "").strip().lower()
    selected = _MODEL_PROFILES.get((profile.name, name), profile)
    if name in _THINKING_DISABLED_EFFORTS and selected.thinking_disabled_efforts is None:
        selected = replace(
            selected,
            thinking_disabled_efforts=_THINKING_DISABLED_EFFORTS[name],
            thinking_budget_tokens=None,
            thinks_by_default=True,
        )
    if name in _ANTHROPIC_BLOCK_BINDING and selected.anthropic_block_binding is None:
        selected = replace(selected, anthropic_block_binding=_ANTHROPIC_BLOCK_BINDING[name])
    if name in _ANTHROPIC_MID_SYSTEM and selected.anthropic_mid_system is None:
        selected = replace(selected, anthropic_mid_system=True)
    if (name in {"claude-opus-5-5", "claude-sonnet-5-5", "claude-fable-5-1",
                 "claude-mythos-5-1"}
            and selected.required_tool_choice_projection is None):
        selected = replace(selected, required_tool_choice_projection="auto")
    return replace(
        selected,
        reasoning_effort_policy=selected.reasoning_effort_policy or reasoning_effort_policy(),
    )


def project_tool_choice(choice: str | dict[str, Any], profile: CompatProfile):
    """Apply the declared forced-choice limit without changing host admission."""
    if profile.required_tool_choice_projection == "auto" and (
        choice == "required" or (
            isinstance(choice, dict) and set(choice) == {"type", "function"}
            and choice.get("type") == "function" and isinstance(choice.get("function"), dict)
            and set(choice["function"]) == {"name"}
            and isinstance(choice["function"]["name"], str) and choice["function"]["name"].strip()
        )
    ):
        return "auto"
    return choice


def compat_profile_digest(profile: CompatProfile) -> str:
    """Version effective policy, excluding prose notes and endpoint secrets."""
    values = asdict(profile)
    if profile.reasoning_effort_policy is None:
        values["reasoning_effort_policy"] = asdict(reasoning_effort_policy())
    values.pop("note", None)
    if profile.required_tool_choice_projection is None:
        values.pop("required_tool_choice_projection")
    if profile.anthropic_block_binding is None:
        values.pop("anthropic_block_binding")
    if profile.anthropic_mid_system is None:
        values.pop("anthropic_mid_system")
    return hashlib.sha256(
        json.dumps(values, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def prefer_buffered_transport(*, base_url: str = "", model: str = "") -> bool:
    """该端点是否应优先走缓冲（非流式）请求。

    这是"端点声明的属性"，不是按模型名的代码分支；模型名只用于选中档位。
    默认 False（流式优先）。
    """

    return compat_profile(base_url=base_url, model=model).prefer_buffered_transport


def stream_idle_timeout(
    *,
    base_url: str = "",
    model: str = "",
    fallback: int | None = None,
) -> int | None:
    """该端点声明的"增量空闲进度"超时（秒）；未声明时返回 ``fallback``。"""

    declared = compat_profile(base_url=base_url, model=model).stream_idle_timeout_seconds
    if declared is None:
        return fallback
    return max(1, int(declared))


def is_thinking_model(model: str, profile: CompatProfile) -> bool:
    name = str(model or "").lower().replace("_", "-")
    return any(hint in name for hint in profile.thinking_model_hints)


def default_delegation_reasoning_effort(
    profile: CompatProfile,
    *,
    model: str,
) -> str | None:
    """Return the declared default for the controller's delegation decision.

    This is deliberately narrower than a model-wide default: final synthesis
    and ordinary tool work keep the provider/user setting.  The Harness only
    uses this value when ``delegate_task`` is in the disclosed tool surface.
    """

    name = str(model or "").strip().lower().replace("_", "-")
    for prefix, effort in profile.delegation_reasoning_effort_rules:
        if name.startswith(prefix):
            return effort
    return None


def request_reasoning_effort(
    profile: CompatProfile,
    *,
    model: str,
    purpose: str,
    requested: str | None,
) -> str | None:
    """Resolve host-declared call purpose with explicit settings first.

    Direct Native and Studio Gateway consume this same pure policy. Unknown
    endpoint/model pairs keep their original options; no hidden retry, model
    switch, output-cap increase or reasoning-to-summary conversion occurs.
    """
    if not profile.supports_reasoning_effort:
        return None
    if requested is not None or purpose != "compaction":
        return clamp_reasoning_effort(model, requested, policy=profile.reasoning_effort_policy)
    name = str(model or "").strip().lower().replace("_", "-")
    for measured_model, effort in profile.compaction_reasoning_effort_rules:
        if name == measured_model:
            return clamp_reasoning_effort(model, effort, policy=profile.reasoning_effort_policy)
    return None


def request_thinking_disabled(
    profile: CompatProfile, *, model: str, purpose: str, requested: str | None,
) -> bool:
    """Project a measured summary setting without overriding explicit effort.

    Compaction is admitted by the host. Never infer it from empty tools or
    natural-language instructions. Ordinary tasks and unknown pairs retain
    their original model settings.
    """
    if purpose != "compaction" or requested is not None or not profile.supports_thinking_field:
        return False
    name = str(model or "").strip().lower().replace("_", "-")
    return name in profile.compaction_thinking_disabled_models and (
        profile.thinking_disabled_efforts is None or None in profile.thinking_disabled_efforts
    )


def apply_chat_compat(
    payload: Mapping[str, Any],
    profile: CompatProfile,
    *,
    model: str = "",
) -> dict[str, Any]:
    """按档位清洗 chat completions 请求体（纯函数，不修改入参）。"""

    result: dict[str, Any] = {k: v for k, v in payload.items()}

    if not profile.supports_stream_usage_option:
        stream_options = result.get("stream_options")
        if isinstance(stream_options, dict):
            cleaned = {k: v for k, v in stream_options.items() if k != "include_usage"}
            if cleaned:
                result["stream_options"] = cleaned
            else:
                result.pop("stream_options", None)

    if not profile.supports_thinking_field:
        result.pop("thinking", None)
    if not profile.supports_reasoning_effort:
        result.pop("reasoning_effort", None)
    if result.get("tool_choice") == "required" and profile.required_tool_choice_projection:
        result["tool_choice"] = profile.required_tool_choice_projection

    if profile.reasoning_echo != "always-preserve":
        drop_all = profile.reasoning_echo == "strip"
        drop_non_thinking = profile.reasoning_echo == "drop-on-non-thinking"
        if drop_all or (drop_non_thinking and not is_thinking_model(model, profile)):
            messages = result.get("messages")
            if isinstance(messages, list):
                result["messages"] = [
                    (
                        {k: v for k, v in message.items() if k != "reasoning_content"}
                        if isinstance(message, dict) and message.get("role") == "assistant"
                        else message
                    )
                    for message in messages
                ]

    return result


def required_output_budget(
    profile: CompatProfile,
    requested: int | None,
    *,
    provider_max: int | None = None,
) -> int | None:
    """在"无法关思考"的端点上，保证输出预算不被思维链吃光。

    不变量：``max_output_tokens > thinking_budget_tokens``（Claude Code 的
    硬约束）。能关思考的端点由 ``should_disable_thinking`` 处理；不能关的
    （实测 GLM 网关拒绝 thinking 字段）只能**抬高预算**：
    ``thinking_budget + content_reserve``。

    返回应发送的输出预算；``None`` 表示不需要调整（保持 requested）。
    """

    preferred = profile.preferred_output_tokens
    if preferred is None:
        return requested
    if requested is None:
        # 调用方没给预算时不擅自新增字段，交给端点默认。
        return None
    if requested >= preferred:
        return requested
    # slot 优化：小预算抬到推荐值（默认 8k），既给思考留空间，又不过度预留容量。
    # 真正的"超长输出"由触顶后的一次性升级处理（model_client 的 length retry）。
    if provider_max is not None and provider_max > 0:
        preferred = min(preferred, provider_max)
    return max(requested, preferred)


def should_disable_thinking(
    profile: CompatProfile,
    *,
    tools_present: bool,
    max_output_tokens: int | None,
    reasoning_effort: str | None = None,
) -> bool:
    """本回合的关闭思考策略（实际投影还须允许该字段）。

    若模型声明 disabled 的 effort 值域，必须命中已知有效档位；不能
    从 None 猜测 provider 默认值，也不能把字段支持当作 disabled 支持。

    规则（任一命中即关）：
    1. 端点不支持 ``thinking`` 字段；
    2. 档位声明"工具回合关思考"且本回合带了工具；
    3. **预算不变量**：端点默认思考且已知思考预算，而本次输出预算不足以
       容纳思考预算 —— 否则始终思考的模型会把预算全花在 reasoning 上，
       正文与工具调用为 0（2026-09-18 实测：三次调用 output 2048 /
       reasoning 2048，各耗 100-160 秒且零产出）。
    """

    if not profile.supports_thinking_field:
        return True
    if (
        profile.thinking_disabled_efforts is not None
        and reasoning_effort not in profile.thinking_disabled_efforts
    ):
        return False
    if profile.disable_thinking_for_tools and tools_present:
        return True
    budget = profile.thinking_budget_tokens
    if (
        profile.thinks_by_default
        and budget is not None
        and max_output_tokens is not None
        and max_output_tokens <= budget
    ):
        return True
    return False


def with_thinking_disabled(
    payload: Mapping[str, Any],
    profile: CompatProfile,
    *,
    reasoning_effort: str | None = None,
) -> dict[str, Any]:
    """Copy with disabled only when the declared field and value domain allow it."""

    if not profile.supports_thinking_field or (
        profile.thinking_disabled_efforts is not None
        and reasoning_effort not in profile.thinking_disabled_efforts
    ):
        return dict(payload)
    return {**payload, "thinking": {"type": "disabled"}}


__all__ = [
    "COMPAT_PROFILES",
    "DEFAULT_COMPAT_RULE",
    "REASONING_ECHO_MODES",
    "CompatProfile",
    "apply_chat_compat",
    "compat_profile",
    "compat_profile_digest",
    "default_delegation_reasoning_effort",
    "request_reasoning_effort",
    "request_thinking_disabled",
    "is_thinking_model",
    "prefer_buffered_transport",
    "required_output_budget",
    "resolve_compat_rule",
    "should_disable_thinking",
    "stream_idle_timeout",
    "with_thinking_disabled",
]

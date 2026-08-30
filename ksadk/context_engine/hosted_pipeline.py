"""Hosted Pipeline —— 把已建模块接成 ksadk_hosted 真实链路（方案 §11.1 / §4.4）。

这是把 Prompt Compiler → Contributors → Context Planner → Context Assembler 串成一条
真实链路的编排器：从 ``PreparedConversationTurn`` 取已编译的 CompiledPrompt、history、
user_input、working_state，运行 Contributors 产出候选 ContextItem，交给 ContextPlanner 做预算
决策，再由 ContextAssembler 投影成最终 Chat 输入。返回 ``(ContextPlan, AssembledInput)``。

**门控**：AgentVersion ``context.rollout.contextEngine=enabled`` 开启，环境变量
``KSADK_CONTEXT_ENGINE_V2_ENABLED=false`` 可作为全局紧急关闭。只有
``prompt_integration_mode=="ksadk_hosted"`` 才由 ``build_run_input`` 调用。本模块纯计算 + 受控
Contributor 调用，不接触 Session Store、不调模型；replan 由调用方在 compaction/PTL 后重新调用
（ADR-016：每 Turn 只生成一份 canonical Plan）。

assisted/native 路径不调用本模块（方案 §6.2）：framework_assisted 由 Adapter 投影，native_runtime
保留原生 history/compaction，KsADK 不重复注入完整 Transcript、不运行第二套 compaction。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Mapping

from ksadk.context_engine.assembler import AssembledInput, ContextAssembler
from ksadk.context_engine.contributors import (
    ContextContributionRequest,
    ContextContributor,
    ContributionResult,
    MemoryRecallContributor,
    WorkspaceRulesContributor,
    run_contributors,
)
from ksadk.context_engine.models import ContextItem
from ksadk.context_engine.planner import ContextPlanner, build_budget
from ksadk.context_engine.policies import ContextPolicy
from ksadk.context_engine.tokenizer import get_default_token_counter


def hosted_pipeline_enabled(*, rollout: str | None = None) -> bool:
    """解析全局 kill switch 与 AgentVersion 级 Context rollout。

    传入 rollout 时，``enabled`` 开启真实链路，``off``/``shadow`` 不改变 Runner
    输入。环境变量仍是最高优先级的紧急开关：显式 false 一律关闭；旧调用未传
    rollout 时则保持原语义，只有环境变量显式 true 才开启。
    """
    raw = os.environ.get("KSADK_CONTEXT_ENGINE_V2_ENABLED")
    normalized = str(raw or "").strip().lower()
    if normalized in {"0", "false", "no", "off"}:
        return False
    env_enabled = normalized in {"1", "true", "yes", "on"}
    if rollout is not None:
        return str(rollout).strip().lower() == "enabled" and (raw is None or env_enabled)
    return env_enabled


def _env_flag(name: str, default: bool = True) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return str(raw).strip().lower() not in {"0", "false", "no", "off"}


@dataclass(frozen=True)
class HostedPipelineResult:
    """hosted pipeline 的产物。``plan`` 为 plain dict 投影，``assembled`` 为 AssembledInput。"""

    plan: dict[str, Any]
    assembled: AssembledInput
    contributor_status: dict[str, str]


def _history_to_items(history: list[dict[str, str]]) -> list[ContextItem]:
    """把投影后的 history rounds 转成 ContextItem。

    user+assistant 绑定为同一原子组（方案 §8.1 group_id），避免长 User 被跳过而
    短 Assistant 留下成为孤儿历史。
    """
    counter = get_default_token_counter()
    items: list[ContextItem] = []
    round_index = 0
    for index, turn in enumerate(history):
        if not isinstance(turn, Mapping):
            continue
        role = str(turn.get("role") or turn.get("author") or "assistant")
        content = turn.get("content") or turn.get("text") or ""
        if not content:
            continue
        text = content if isinstance(content, str) else str(content)
        # user 开启新 round；assistant 继承上一个 round（与 user 同组）
        if role == "user":
            round_index += 1
        items.append(
            ContextItem(
                item_id=f"hist:{index}",
                kind="history_round",
                content=text,
                source="transcript",
                trust_level="developer",
                priority=0,
                estimated_tokens=counter.count_text(text),
                required=False,
                droppable=True,
                group_id=f"round:{round_index}",
                seq_start=index,
                metadata={
                    "role": role if role in ("user", "assistant", "model") else "assistant",
                },
            )
        )
    return items


def _working_state_to_item(working_state: Mapping[str, Any] | None) -> ContextItem | None:
    """把 WorkingState 审计 dict 转成 ContextItem（方案 §9.3 重注入）。"""
    if not isinstance(working_state, Mapping) or not working_state:
        return None
    # 复用 runtime_input 的渲染，保证 XML 格式与重注入一致。
    try:
        from ksadk.conversations.runtime_input import _render_working_state_xml

        xml = _render_working_state_xml(working_state)
    except Exception:  # noqa: BLE001
        return None
    if not xml:
        return None
    counter = get_default_token_counter()
    return ContextItem(
        item_id="working_state",
        kind="working_state",
        content=xml,
        source="checkpoint",
        trust_level="developer",
        priority=0,
        estimated_tokens=counter.count_text(xml),
        required=False,  # WorkingState 高优先级但非强制 required（可被降级，方案 §8.4 第 7）
        droppable=True,
        stable=False,
        metadata={"content_hash": working_state.get("content_hash")},
    )


def _compiled_prompt_to_item(compiled_prompt: Mapping[str, Any] | None) -> ContextItem | None:
    """把真实 CompiledPrompt dict（含 prompt_content）转成 required ContextItem。"""
    if not isinstance(compiled_prompt, Mapping):
        return None
    content = compiled_prompt.get("prompt_content")
    if not isinstance(content, str) or not content.strip():
        return None
    counter = get_default_token_counter()
    return ContextItem(
        item_id="compiled_prompt",
        kind="compiled_prompt",
        content=content,
        source="prompt_compiler",
        trust_level="platform",  # compiled_prompt 含 platform_safety（若编译含）
        priority=0,
        estimated_tokens=int(
            compiled_prompt.get("prompt_estimated_tokens") or counter.count_text(content)
        ),
        required=True,
        droppable=False,
        stable=True,
        content_hash=compiled_prompt.get("prompt_content_hash"),
    )


def _current_input_to_item(user_input: str) -> ContextItem | None:
    text = str(user_input or "").strip()
    if not text:
        return None
    counter = get_default_token_counter()
    return ContextItem(
        item_id="current_input",
        kind="current_input",
        content=text,
        source="user",
        trust_level="user",
        priority=0,
        estimated_tokens=counter.count_text(text),
        required=True,
        droppable=False,
    )


def default_hosted_contributors(
    *,
    policy: ContextPolicy | None = None,
    user_id: str = "",
    agent_id: str = "",
    memory_provider: Any = None,
    memory_recall_enabled: bool | None = None,
) -> list[ContextContributor]:
    """构造默认 hosted Contributors（方案 §8.7 首批内置）。

    - ``MemoryRecallContributor``：仅当 ``KSADK_MEMORY_ENABLED`` 且有可用 Provider 时启用。
    - ``WorkspaceRulesContributor``：受 ``KSADK_PROMPT_AUTO_DISCOVERY`` 门控（默认关）。
    SkillManifest/Attachment Contributor 需要外部 manifests/附件，留调用方按需注入。

    返回的 Contributor 一律 trust_level 不高于注册配置（external = untrusted），Planner 拥有
    是否进入请求的最终决策。
    """
    pol = policy or ContextPolicy.from_env()
    contributors: list[ContextContributor] = []
    memory_enabled = pol.memory.enabled
    if memory_recall_enabled is not None:
        memory_enabled = bool(memory_recall_enabled)
        # 环境级 false 保留为生产紧急 kill switch。
        if os.environ.get("KSADK_MEMORY_ENABLED", "").strip().lower() in {
            "0",
            "false",
            "off",
        }:
            memory_enabled = False
    if memory_enabled:
        try:
            from ksadk.memory.coordinator import MemoryCoordinator

            if memory_provider is None:
                from ksadk.memory.providers.local_sqlite import resolve_default_memory_provider

                memory_provider = resolve_default_memory_provider()
            coordinator = MemoryCoordinator(
                memory_provider,
                tenant_id="local",
                workspace_id="local",
            )
            contributors.append(
                MemoryRecallContributor(
                    coordinator,
                    max_tokens=pol.memory.recall_max_tokens,
                    top_k=pol.memory.recall_top_k,
                    min_score=pol.memory.min_score,
                )
            )
        except Exception:  # noqa: BLE001 — Provider 构造失败不应阻断 hosted 链路
            pass
    if pol.prompt.auto_discovery:
        try:
            contributors.append(
                WorkspaceRulesContributor(
                    max_tokens=pol.prompt.rule_files_max_tokens,
                )
            )
        except Exception:  # noqa: BLE001
            pass
    return contributors


async def run_hosted_pipeline(
    *,
    compiled_prompt: Mapping[str, Any] | None,
    user_input: str,
    history: list[dict[str, str]],
    working_state: Mapping[str, Any] | None,
    model_metadata: Mapping[str, Any] | None,
    contributors: list[ContextContributor] | None = None,
    policy: ContextPolicy | None = None,
    integration_mode: str = "ksadk_hosted",
    accounting_accuracy: str = "estimated",
    session_id: str = "",
    invocation_id: str = "",
    user_id: str = "",
    agent_id: str = "",
    agent_max_input_tokens: int | None = None,
    agent_reserve_output_tokens: int | None = None,
) -> HostedPipelineResult | None:
    """运行真实 hosted 链路（方案 §11.1 细化时序 1-10）。

    ``agent_max_input_tokens``/``agent_reserve_output_tokens``：AgentVersion 的 ContextSpec
    预算覆盖（方案 §8.2）。非 None 时优先于 model_metadata 的窗口（解决 AgentVersion 预算
    没传到 Planner 的问题）。返回 ``None`` 表示无可组装内容。
    """
    pol = policy or ContextPolicy.from_env()
    counter = get_default_token_counter()

    # 1. 基础候选：compiled_prompt(required) + current_input(required) + history rounds + working_state  # noqa: E501
    candidates: list[ContextItem] = []
    prompt_item = _compiled_prompt_to_item(compiled_prompt)
    if prompt_item is not None:
        candidates.append(prompt_item)
    input_item = _current_input_to_item(user_input)
    if input_item is not None:
        candidates.append(input_item)
    candidates.extend(_history_to_items(history))
    ws_item = _working_state_to_item(working_state)
    if ws_item is not None:
        candidates.append(ws_item)

    # 2. 运行 Contributors（Memory Recall 等）产出 untrusted 候选（方案 §8.7）
    contrib_status: dict[str, str] = {}
    if contributors:
        request = ContextContributionRequest(
            user_input=user_input,
            session_id=session_id,
            invocation_id=invocation_id,
            user_id=user_id,
            agent_id=agent_id,
        )
        result: ContributionResult = await run_contributors(
            contributors,
            request,
            default_timeout_ms=pol.contributors.default_timeout_ms,
            default_failure_mode=pol.contributors.default_failure_mode,
        )
        contrib_status = dict(result.status)
        candidates.extend(result.items)

    if not any(i.kind == "compiled_prompt" for i in candidates) and not input_item:
        return None

    # 3. 构造预算（方案 §8.2）。优先用 AgentVersion 的 ContextSpec 预算
    # （agent_max_input_tokens），缺失时 fallback 到 model_metadata。
    if agent_max_input_tokens is not None and agent_max_input_tokens > 0:
        # AgentVersion 预算：max_input_tokens 直接作为 context_window，reserve 从 spec 取。
        # 不扣 safety_buffer（AgentVersion 已显式指定预算，8000 默认 buffer 是为百万
        # token 窗口设计的，在小预算下会导致 max_input=0）。
        from dataclasses import replace

        reserve_out = agent_reserve_output_tokens or 0
        agent_policy = replace(pol.budget, safety_buffer_tokens=0)
        budget = build_budget(
            policy=agent_policy,
            context_window_tokens=agent_max_input_tokens + reserve_out,
            reserved_output_tokens=reserve_out,
            reserved_reasoning_tokens=0,
        )
    else:
        from ksadk.conversations.model_context import (
            get_effective_context_window_tokens,
        )

        max_input = get_effective_context_window_tokens(model_metadata)
        budget = build_budget(
            policy=pol.budget,
            context_window_tokens=max_input + pol.budget.safety_buffer_tokens,
            reserved_output_tokens=0,
            reserved_reasoning_tokens=0,
        )

    # 4. Planner 决策（方案 §8.4）
    planner = ContextPlanner(policy=pol.budget)
    plan = planner.plan(
        candidates,
        budget=budget,
        integration_mode=integration_mode,
        accounting_accuracy=accounting_accuracy,
        tokenizer=counter.name,
        stable_prefix_hash=str((compiled_prompt or {}).get("prompt_stable_prefix_hash") or ""),
    )

    # 5. Assembler 投影成 Chat 输入（方案 §8）
    assembled = ContextAssembler().assemble_chat(plan)
    plan_dict = _plan_to_dict(plan)
    # hosted 模式由 KsADK 拥有最终 Runner payload，因此 assembler 的 token 结果就是
    # projected 口径；actual 仍只接受 Runtime/Provider usage 回填。
    plan_dict["projected_input_tokens"] = assembled.estimated_tokens

    return HostedPipelineResult(
        plan=plan_dict,
        assembled=assembled,
        contributor_status=contrib_status,
    )


def _plan_to_dict(plan: Any) -> dict[str, Any]:
    """ContextPlan → plain dict 投影（供 trace / payload 接管 / 后续 usage 回填）。"""
    from dataclasses import asdict

    d = asdict(plan)
    # 冻结决策审计：selected 只记 id/kind/tokens，不记 content（明文不进 trace，方案 §19）
    d["selected"] = [
        {
            "item_id": i.get("item_id"),
            "kind": i.get("kind"),
            "estimated_tokens": i.get("estimated_tokens"),
            "group_id": i.get("group_id"),
        }
        for i in d.get("selected", [])
    ]
    return d


def assembled_to_payload(assembled: AssembledInput) -> dict[str, Any]:
    """把 AssembledInput 投影成 runner payload 的 instructions/input/history（方案 §8）。

    - ``system`` → payload["instructions"]
    - 最后一条 user message → payload["input"]
    - 其余 messages（system 之后、最后 user 之前）→ payload["history"]（runner _to_state 消费）
    """
    messages = list(assembled.messages)
    system = assembled.system
    # 分离：第一条 system，最后一条 user 作为 input，其余作为 history
    history: list[dict[str, Any]] = []
    input_text = ""
    non_system = [m for m in messages if m.get("role") != "system"]
    if non_system:
        last = non_system[-1]
        if last.get("role") == "user":
            input_text = str(last.get("content") or "")
            history = non_system[:-1]
        else:
            history = non_system
    else:
        history = []
    return {
        "instructions": system,
        "input": input_text,
        "history": history,
    }


__all__ = [
    "HostedPipelineResult",
    "assembled_to_payload",
    "hosted_pipeline_enabled",
    "run_hosted_pipeline",
]

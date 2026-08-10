"""Context Assembler —— 把 ContextPlan.selected 投影成 messages/responses 输入（方案 §8）。

Assembler 是 KsADK-owned（``ksadk_hosted``）路径的最终输入组装器：把 ``ContextPlan.selected``
按方案 §7.4 的稳定前缀→部署级→动态后缀顺序投影成 Chat/Responses 格式。assisted/native 路径
不调用本模块，由 RuntimeAdapter 自行投影（方案 §6.2）。

第一个版本只实现 Chat messages 与 Responses items 两种合法投影，不含模型调用；actual_token
由调用方在收到 usage 后回填 ``ContextPlan.runtime_reported_input_tokens``。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from ksadk.context_engine.models import ContextItem, ContextPlan

ProjectionFormat = Literal["chat", "responses"]


@dataclass(frozen=True)
class AssembledInput:
    """组装后的模型输入（shadow/可观测用，不直接发模型）。"""

    format: ProjectionFormat
    system: str
    messages: list[dict[str, Any]]
    responses_items: list[dict[str, Any]]
    estimated_tokens: int
    warnings: tuple[str, ...] = ()


def _item_text(item: ContextItem) -> str:
    content = item.content
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict):
                t = part.get("text") or part.get("content")
                if isinstance(t, str):
                    parts.append(t)
        return "\n".join(parts)
    return str(content or "")


def _split_prompt_and_rest(selected: list[ContextItem]) -> tuple[str, list[ContextItem]]:
    """稳定前缀（compiled_prompt）单独提为 system，其余按顺序进入 messages。"""
    prompt_text = ""
    rest: list[ContextItem] = []
    for item in selected:
        if item.kind == "compiled_prompt":
            prompt_text = (
                (prompt_text + "\n\n" + _item_text(item)).strip("\n\n")
                if prompt_text
                else _item_text(item)
            )
        else:
            rest.append(item)
    return prompt_text, rest


def _role_for(item: ContextItem) -> str:
    if item.kind == "current_input":
        return "user"
    if item.kind == "history_round":
        # history_round 的 content 形如 {"role": "...", "content": ...} 或带 role metadata
        role = item.metadata.get("role")
        if isinstance(role, str):
            return role
        return "assistant"
    if item.kind == "tool_result":
        return "tool"
    return "assistant"


def _current_input_last(items: list[ContextItem]) -> list[ContextItem]:
    """Keep the canonical current input at the end of Chat chronology.

    Planner order represents retention priority, not physical message order. If
    ``current_input`` is projected before selected history, the runner can treat
    it as history and inject it again as the new input. Preserve every other
    item's relative order and move only ``current_input`` to the end.
    """

    return [item for item in items if item.kind != "current_input"] + [
        item for item in items if item.kind == "current_input"
    ]


class ContextAssembler:
    """把 ContextPlan 投影成 Chat/Responses 输入（方案 §8）。

    纯函数式、无副作用。``assemble_chat`` 输出 OpenAI Chat 风格 messages；
    ``assemble_responses`` 输出 Responses API items。两者共用同一 selected 顺序。
    """

    def assemble_chat(self, plan: ContextPlan) -> AssembledInput:
        system, rest = _split_prompt_and_rest(plan.selected)
        rest = _current_input_last(rest)
        messages: list[dict[str, Any]] = []
        if system:
            messages.append({"role": "system", "content": system})
        warnings: list[str] = []
        for item in rest:
            role = _role_for(item)
            content = _item_text(item)
            if item.metadata.get("truncated_to_tokens") is not None:
                warnings.append(f"{item.item_id}:truncated")
            if item.metadata.get("replaced_with_artifact_summary"):
                warnings.append(f"{item.item_id}:artifact_summary")
            messages.append({"role": role, "content": content, "name": item.metadata.get("name")})
        return AssembledInput(
            format="chat",
            system=system,
            messages=messages,
            responses_items=[],
            estimated_tokens=plan.planned_input_tokens,
            warnings=tuple(warnings),
        )

    def assemble_responses(self, plan: ContextPlan) -> AssembledInput:
        system, rest = _split_prompt_and_rest(plan.selected)
        rest = _current_input_last(rest)
        items: list[dict[str, Any]] = []
        if system:
            items.append(
                {
                    "type": "message",
                    "role": "system",
                    "content": [{"type": "input_text", "text": system}],
                }
            )
        warnings: list[str] = []
        for item in rest:
            role = _role_for(item)
            content = _item_text(item)
            if item.metadata.get("truncated_to_tokens") is not None:
                warnings.append(f"{item.item_id}:truncated")
            if item.kind == "tool_result":
                # Responses function_call_output
                call_id = str(
                    item.metadata.get("call_id") or item.metadata.get("tool_call_id") or ""
                )
                items.append(
                    {"type": "function_call_output", "call_id": call_id, "output": content}
                )
            else:
                item_type = "input_text" if role == "user" else "output_text"
                items.append(
                    {
                        "type": "message",
                        "role": role,
                        "content": [{"type": item_type, "text": content}],
                    }
                )
        return AssembledInput(
            format="responses",
            system=system,
            messages=[],
            responses_items=items,
            estimated_tokens=plan.planned_input_tokens,
            warnings=tuple(warnings),
        )


def assemble(plan: ContextPlan, *, fmt: ProjectionFormat = "chat") -> AssembledInput:
    """便捷入口。"""
    asm = ContextAssembler()
    return asm.assemble_chat(plan) if fmt == "chat" else asm.assemble_responses(plan)


__all__ = ["AssembledInput", "ContextAssembler", "ProjectionFormat", "assemble"]

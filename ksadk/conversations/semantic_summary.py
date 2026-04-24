from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

import httpx

from ksadk.configs.settings import settings
from ksadk.conversations.compaction_prompt import (
    build_compaction_prompt_messages,
    extract_summary_text,
)
from ksadk.conversations.context import canonical_event_type, extract_event_text, summarize_event_groups
from ksadk.sessions.base import SessionEvent

SUMMARY_VERSION = "v1"
DEFAULT_COMPACTION_SUMMARY_TIMEOUT_MS = 45_000
DEFAULT_COMPACTION_SUMMARY_MAX_GROUPS = 12
SUMMARY_PREFIX = "Earlier conversation summary:"


@dataclass
class CompactionSummaryResult:
    """一次 checkpoint 摘要的标准结果。"""

    summary_text: str
    summary_strategy: str
    summary_version: str = SUMMARY_VERSION
    summary_model: str = ""
    summary_usage: dict[str, Any] = field(default_factory=dict)
    fallback_reason: str | None = None


class SummaryModelClient:
    """独立的摘要模型客户端。

    注意这里故意不复用 agent runner：
    - runner 的职责是执行用户 agent；
    - 摘要器的职责是平台内部的 context compaction。
    两者拆开后，失败隔离和后续替换模型都会更清晰。
    """

    def __init__(
        self,
        *,
        api_base: str | None = None,
        api_key: str | None = None,
    ):
        self.api_base = str(api_base or settings.model.api_base or "").rstrip("/")
        self.api_key = str(api_key or settings.model.api_key or "").strip()

    @property
    def is_available(self) -> bool:
        return bool(self.api_base and self.api_key)

    def _chat_completions_url(self) -> str:
        if self.api_base.endswith("/v1"):
            return f"{self.api_base}/chat/completions"
        return f"{self.api_base}/v1/chat/completions"

    async def summarize(
        self,
        *,
        model: str,
        messages: Sequence[dict[str, str]],
        timeout_ms: int,
    ) -> tuple[str, dict[str, Any]]:
        if not self.is_available:
            raise RuntimeError("summary model client is not configured")
        if not model:
            raise RuntimeError("summary model is not configured")

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }
        payload = {
            "model": model,
            "messages": list(messages),
            "stream": False,
            "temperature": 0,
        }
        timeout_seconds = max(1.0, float(timeout_ms) / 1000.0)
        async with httpx.AsyncClient(timeout=timeout_seconds) as client:
            response = await client.post(self._chat_completions_url(), headers=headers, json=payload)
            response.raise_for_status()
            data = response.json()

        choices = data.get("choices") or []
        message = choices[0].get("message") if choices else {}
        content = message.get("content") if isinstance(message, Mapping) else ""
        if isinstance(content, list):
            fragments: list[str] = []
            for item in content:
                if isinstance(item, Mapping) and item.get("text"):
                    fragments.append(str(item["text"]))
            content = "\n".join(fragment for fragment in fragments if fragment)
        text = str(content or "").strip()
        if not text:
            raise RuntimeError("summary model returned empty content")
        return text, dict(data.get("usage") or {})


def resolve_summary_model_client() -> SummaryModelClient:
    return SummaryModelClient()


def _ensure_summary_prefix(summary_text: str) -> str:
    text = str(summary_text or "").strip()
    if not text or text.startswith(SUMMARY_PREFIX):
        return text
    return f"{SUMMARY_PREFIX}\n{text}"


def _env_truthy(name: str) -> bool:
    value = str(os.getenv(name, "")).strip().lower()
    return value in {"1", "true", "yes", "on"}


def semantic_compaction_disabled() -> bool:
    return _env_truthy("COMPACTION_DISABLE_SEMANTIC")


def get_summary_timeout_ms() -> int:
    raw = os.getenv("COMPACTION_SUMMARY_TIMEOUT_MS", "").strip()
    try:
        return max(1_000, int(raw))
    except (TypeError, ValueError):
        return DEFAULT_COMPACTION_SUMMARY_TIMEOUT_MS


def get_summary_max_groups() -> int:
    raw = os.getenv("COMPACTION_SUMMARY_MAX_GROUPS", "").strip()
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return DEFAULT_COMPACTION_SUMMARY_MAX_GROUPS


def resolve_summary_model(current_model: str | None) -> str:
    override = str(os.getenv("COMPACTION_SUMMARY_MODEL", "")).strip()
    if override:
        return override
    if current_model:
        return str(current_model)
    return str(settings.model.model_name or "")


def find_pinned_group_indexes(groups: Sequence[Sequence[SessionEvent]]) -> set[int]:
    """找出不能被 compact 的轮次。

    当前先严格保护两类未完成状态：
    1. approval_request 尚未收到 approval_response
    2. tool_call 尚未看到 tool_result
    """

    pending_approval_groups: list[int] = []
    pending_tool_groups: list[int] = []
    for index, group in enumerate(groups):
        for event in group:
            event_type = canonical_event_type(
                event.event_type,
                author=event.author,
                role=str((event.content or {}).get("role") or ""),
            )
            if event_type == "approval_request":
                pending_approval_groups.append(index)
                continue
            if event_type == "approval_response" and pending_approval_groups:
                pending_approval_groups.pop()
                continue
            if event_type == "tool_call":
                pending_tool_groups.append(index)
                continue
            if event_type == "tool_result" and pending_tool_groups:
                pending_tool_groups.pop()
    return set(pending_approval_groups + pending_tool_groups)


def extract_pinned_state(groups: Sequence[Sequence[SessionEvent]]) -> dict[str, Any]:
    """提取必须在 checkpoint 里显式保留的状态。"""

    pending_approvals: list[str] = []
    pending_tools: list[str] = []
    attachment_refs: list[str] = []
    current_user_goal = ""

    for group in groups:
        for event in group:
            event_type = canonical_event_type(
                event.event_type,
                author=event.author,
                role=str((event.content or {}).get("role") or ""),
            )
            text = extract_event_text(event)
            if event_type == "approval_request" and text:
                pending_approvals.append(text)
            elif event_type == "approval_response" and pending_approvals:
                pending_approvals.pop()
            elif event_type == "tool_call" and text:
                pending_tools.append(text)
            elif event_type == "tool_result" and pending_tools:
                pending_tools.pop()
            elif event_type == "attachment_ref":
                attachment_refs.append(text)
            elif event_type == "user_message" and text:
                current_user_goal = text
                for attachment in (event.metadata or {}).get("attachments") or []:
                    if not isinstance(attachment, Mapping):
                        continue
                    label = str(
                        attachment.get("display_name")
                        or attachment.get("file_uri")
                        or attachment.get("storage_path")
                        or ""
                    ).strip()
                    if label:
                        attachment_refs.append(label)

    unique_attachments: list[str] = []
    for item in attachment_refs:
        normalized = str(item or "").strip()
        if normalized and normalized not in unique_attachments:
            unique_attachments.append(normalized)

    return {
        "pending_approvals": pending_approvals,
        "pending_tools": pending_tools,
        "attachment_refs": unique_attachments[-5:],
        "current_user_goal": current_user_goal,
    }


def _build_semantic_input(
    *,
    previous_summary: str,
    groups_to_compact: Sequence[Sequence[SessionEvent]],
    pinned_state: Mapping[str, Any],
    model_metadata: Mapping[str, Any] | None,
) -> tuple[str, list[list[SessionEvent]]]:
    max_groups = get_summary_max_groups()
    selected_groups = [list(group) for group in groups_to_compact]
    merged_previous_summary = previous_summary.strip()
    if len(selected_groups) > max_groups:
        skipped_groups = selected_groups[:-max_groups]
        selected_groups = selected_groups[-max_groups:]
        skipped_summary = summarize_event_groups(skipped_groups, previous_summary=merged_previous_summary)
        merged_previous_summary = skipped_summary
    return merged_previous_summary, selected_groups


async def summarize_compaction(
    *,
    groups_to_compact: Sequence[Sequence[SessionEvent]],
    previous_summary: str,
    pinned_state: Mapping[str, Any],
    model_metadata: Mapping[str, Any] | None,
    model: str | None,
) -> CompactionSummaryResult:
    """语义摘要优先，失败后自动回退到 extractive。"""

    fallback_summary = summarize_event_groups(list(groups_to_compact), previous_summary=previous_summary)
    if semantic_compaction_disabled():
        return CompactionSummaryResult(
            summary_text=fallback_summary,
            summary_strategy="extractive",
            fallback_reason="semantic summarizer disabled",
        )

    summary_model = resolve_summary_model(model)
    client = resolve_summary_model_client()
    if not client.is_available:
        return CompactionSummaryResult(
            summary_text=fallback_summary,
            summary_strategy="extractive",
            fallback_reason="summary model client is not configured",
        )

    try:
        merged_previous_summary, selected_groups = _build_semantic_input(
            previous_summary=previous_summary,
            groups_to_compact=groups_to_compact,
            pinned_state=pinned_state,
            model_metadata=model_metadata,
        )
        prompt_messages = build_compaction_prompt_messages(
            previous_summary=merged_previous_summary,
            groups_to_compact=selected_groups,
            pinned_state=pinned_state,
            model_metadata=model_metadata,
        )
        raw_text, usage = await client.summarize(
            model=summary_model,
            messages=prompt_messages,
            timeout_ms=get_summary_timeout_ms(),
        )
        summary_text = extract_summary_text(raw_text)
        if not summary_text:
            raise RuntimeError("summary model returned empty <summary> block")
        return CompactionSummaryResult(
            summary_text=_ensure_summary_prefix(summary_text),
            summary_strategy="semantic",
            summary_model=summary_model,
            summary_usage=usage,
        )
    except Exception as exc:
        return CompactionSummaryResult(
            summary_text=fallback_summary,
            summary_strategy="extractive",
            fallback_reason=str(exc) or exc.__class__.__name__,
        )

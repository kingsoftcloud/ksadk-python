"""Locked Native Studio token/media planning and effect timeout policies."""

from __future__ import annotations

import json
from collections.abc import Mapping
from functools import partial

from ksadk.harness.native.context_runtime import CompactionPolicy, ContextPlanner
from ksadk.harness.native.model import ModelProfile
from ksadk.harness.native.model_replay import project_model_message
from ksadk.studio.contracts import ContextSpec
from ksadk.toolsets.workspace_read_budget import estimated_source_units


def _estimated_token_counter(text: str) -> int:
    """Versioned fallback for a provider without a compatible tokenizer.

    Studio's ContextSpec window is measured in tokens. Charging UTF-8 bytes
    against that window made long coding runs compact after only ~15k provider
    input tokens on a 64k Build. Reuse the existing CJK-aware estimator with
    a 25% safety margin; the provider's explicit overflow still triggers the
    Native bounded force-compaction path. This is not provider usage.
    """
    return estimated_source_units(text)


def _studio_message_budget(message: dict, *, provider_fields: bool = False) -> int:
    """Charge image blocks explicitly without counting base64 as text history.

    This is a conservative host budget, not a claim about provider token usage.
    ZCode's pinned image budget uses 0.125 units per base64 character; keep a
    minimum reserve for small screenshots whose encoded size understates the
    provider's image framing cost. The original message is never rewritten.
    """
    projected = json.loads(json.dumps(message, ensure_ascii=False))
    reserve = 0
    content = projected.get("content")
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "image_url":
                continue
            source = block.get("image_url")
            url = source.get("url") if isinstance(source, dict) else None
            if not isinstance(url, str):
                raise ValueError("image URL missing from typed content block")
            if url.startswith("data:image/") and "," in url:
                reserve += max(2048, (len(url.partition(",")[2]) + 7) // 8)
            else:
                reserve += 8192
            block["image_url"] = {"url": "[host image budgeted separately]"}
    framing = {"separators": (",", ":")} if provider_fields else {}
    text_cost = _estimated_token_counter(
        json.dumps(projected, ensure_ascii=False, sort_keys=True, **framing)
    )
    return text_cost + reserve


def _studio_model_message_budget(message: dict, *, profile: ModelProfile) -> int:
    return _studio_message_budget(project_model_message(message, profile), provider_fields=True)


def build_studio_context_planner(
    profile: ModelProfile, *, context: ContextSpec | None = None,
    planner_factory=ContextPlanner,
) -> ContextPlanner:
    """One locked token/media policy for parent and child Native Runs."""
    defaults = CompactionPolicy()
    compaction = context.compaction if context is not None else None
    trigger = compaction.threshold_ratio if compaction is not None else defaults.trigger_ratio
    # thresholdRatio is the Build's single-stage trigger, not PCM's soft/hard
    # thresholds. For an earlier trigger, preserve the existing proportional
    # recovery headroom; never enlarge the normal post-summary target.
    target = min(defaults.target_ratio, trigger * defaults.target_ratio / defaults.trigger_ratio)
    return planner_factory(
        tool_media_projection_version=(
            "source-profile-tool-media/v1" if profile.image_input_mode == "text_only" else None
        ),
        count_tokens=_estimated_token_counter,
        count_message_tokens=partial(_studio_model_message_budget, profile=profile),
        tokenizer_id=(
            "studio-cjk-ascii-estimate-125-provider-message-fields-plus-image/v2"
            if profile.image_input_mode == "native"
            else "studio-cjk-ascii-estimate-125-provider-message-fields/v2"
        ),
        summary_budget_unit="estimated tokens",
        # Keep the target separate from provider usage; admit the complete
        # continuation/post-summary request within the locked context window.
        summary_budget_mode="context_bounded_estimate",
        summary_completion_policy="bounded-committed-summary/v1",
        subagent_continuity_policy="accounted_child_runs/v1",
        source_progress_policy="receipt-source-progress/v2",
        # The estimate and provider tokens can differ. Reserve a
        # bounded share of the locked context for the summary.
        # A fixed 2 KiB limit
        # failed a real code repair after the files were edited;
        # that failure must not turn completed tool work into a
        # terminal compaction error on a 32 KiB Build.
        policy=CompactionPolicy(
            enabled=compaction.enabled if compaction is not None else defaults.enabled,
            prune_tool_results=(
                compaction.prune_tool_results
                if compaction is not None else defaults.prune_tool_results
            ),
            trigger_ratio=trigger,
            target_ratio=target,
            fresh_read_max_chars=2000,
            summary_max_tokens=min(
                8192,
                profile.max_output_tokens,
                max(2048, profile.context_window // 8),
            ),
            # In long coding tasks, discarding every recent read
            # after each summary made the Agent re-read the same
            # unchanged files until its model budget was spent.
            # Keep several completed tool batches as a bounded
            # working set; the planner still enforces the hard
            # input capacity and preserves the raw transcript.
            recent_tool_batch_count=4,
            recent_tool_batch_max_tokens=min(12000, profile.context_window // 6),
            recent_tool_batches_total_max_tokens=min(24000, profile.context_window // 4),
        ),
    )


def _native_effect_timeout_seconds(
    main: ModelProfile,
    vision: ModelProfile | None = None,
    *,
    browser_tool_seconds: int = 0,
) -> float:
    """Keep the outer effect lease through browser execution and vision captioning."""
    profiles = (main, vision) if vision is not None else (main,)
    bound = max(120.0, *((item.request_timeout_seconds or 60.0) + 15.0 for item in profiles))
    if browser_tool_seconds:
        caption_seconds = vision.request_timeout_seconds or 60.0 if vision else 0.0
        bound = max(bound, browser_tool_seconds + caption_seconds + 15.0)
    return bound


def _locked_browser_timeout_seconds(build: Mapping[str, object]) -> int:
    capabilities = build.get("capabilities")
    tools = capabilities.get("tools") if isinstance(capabilities, Mapping) else None
    if not isinstance(tools, (list, tuple)):
        return 0
    for tool in tools:
        if (
            isinstance(tool, Mapping)
            and tool.get("name") == "browser_inspect_workspace"
            and tool.get("enabled", True) is not False
        ):
            value = tool.get("timeoutSeconds", 20)
            if type(value) is int and 1 <= value <= 3600:
                return value
            raise ValueError("native_studio_browser_timeout_invalid")
    return 0

from __future__ import annotations

import math
from typing import Any, Mapping

# 这组默认值对应当前金山云模型服务还未完全补齐 metadata 时的保底能力。
# 两周后上游接口扩展后，只需要继续往 normalize_model_metadata 里补映射，
# 前端和 runtime 都不需要改自己的消费方式。
DEFAULT_CONTEXT_WINDOW_TOKENS = 200_000
DEFAULT_MAX_INPUT_TOKENS = 200_000
DEFAULT_MAX_OUTPUT_TOKENS = 32_000
DEFAULT_MAX_REASONING_TOKENS = 32_000
DEFAULT_REQUESTS_PER_MINUTE = 500
DEFAULT_TOKENS_PER_MINUTE = 1_000_000

AUTOCOMPACT_SUMMARY_RESERVE_TOKENS = 20_000
AUTOCOMPACT_BUFFER_TOKENS = 13_000

DEFAULT_MODEL_CAPABILITIES = {
    "function_calling": True,
    "structured_output": True,
    "context_caching": True,
    "multimodal_input_image": False,
    "multimodal_input_video": False,
    "multimodal_input_file": False,
}

DEFAULT_MODEL_LIMITS = {
    "context_window_tokens": DEFAULT_CONTEXT_WINDOW_TOKENS,
    "max_input_tokens": DEFAULT_MAX_INPUT_TOKENS,
    "max_output_tokens": DEFAULT_MAX_OUTPUT_TOKENS,
    "max_reasoning_tokens": DEFAULT_MAX_REASONING_TOKENS,
    "rpm": DEFAULT_REQUESTS_PER_MINUTE,
    "tpm": DEFAULT_TOKENS_PER_MINUTE,
}

DEFAULT_MODEL_PRICING = {
    "online_input_per_million": 4.0,
    "online_output_per_million": 18.0,
    "batch_input_per_million": 2.0,
    "batch_output_per_million": 9.0,
    "online_cache_hit_input_per_million": 1.0,
    "batch_cache_hit_input_per_million": 1.0,
}


def _coerce_positive_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _coerce_token_limit(value: Any, *, assume_k_for_plain_values: bool = False) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        parsed = int(value)
        if assume_k_for_plain_values and 0 < parsed <= 1000:
            return parsed * 1000
        return parsed if parsed > 0 else None

    text = str(value).strip().lower().replace("_", "")
    if not text:
        return None

    multiplier = 1
    if text.endswith("k"):
        multiplier = 1000
        text = text[:-1]
    elif text.endswith("m"):
        multiplier = 1000 * 1000
        text = text[:-1]
    elif text.endswith("b"):
        multiplier = 1000 * 1000 * 1000
        text = text[:-1]

    try:
        parsed = float(text)
    except (TypeError, ValueError):
        return None
    if parsed <= 0:
        return None
    resolved = int(parsed * multiplier)
    if multiplier == 1 and assume_k_for_plain_values and text.isdigit() and 0 < resolved <= 1000:
        return resolved * 1000
    return resolved


def _lookup(raw: Mapping[str, Any], *paths: str) -> Any:
    """按多条路径取 metadata，兼容未来上游字段逐步演进。"""

    for path in paths:
        current: Any = raw
        found = True
        for key in path.split("."):
            if not isinstance(current, Mapping) or key not in current:
                found = False
                break
            current = current[key]
        if found and current is not None:
            return current
    return None


def _normalize_modality_name(raw: Any) -> str:
    text = str(raw or "").strip().lower()
    if not text:
        return ""

    aliases = {
        "文字": "text",
        "文本": "text",
        "text": "text",
        "图片": "image",
        "图像": "image",
        "image": "image",
        "视频": "video",
        "video": "video",
        "文件": "file",
        "文档": "file",
        "file": "file",
    }
    return aliases.get(text, text)


def _extract_input_modalities(raw_model: Mapping[str, Any]) -> list[str]:
    architecture = raw_model.get("architecture")
    if not isinstance(architecture, Mapping):
        return []
    modalities = architecture.get("input_modalities")
    if not isinstance(modalities, list):
        return []

    normalized: list[str] = []
    for item in modalities:
        value = _normalize_modality_name(item)
        if value and value not in normalized:
            normalized.append(value)
    return normalized


def supports_native_image_input(model_metadata: Mapping[str, Any] | None) -> bool:
    if not isinstance(model_metadata, Mapping):
        return False
    capabilities = model_metadata.get("capabilities")
    if isinstance(capabilities, Mapping) and capabilities.get("multimodal_input_image") is not None:
        return bool(capabilities.get("multimodal_input_image"))
    return "image" in _extract_input_modalities(model_metadata)


def estimate_text_tokens(text: str) -> int:
    """轻量 token 估算。

    当前先做一层比 `len/4` 更稳的启发式：
    - CJK 字符按 1 token 估算，避免中文场景长期卡在 0% / 1%
    - 其他字符继续按 4 chars ~= 1 token 估算

    这仍然不是真实 tokenizer，但比纯英文口径更接近本地中文使用体验。
    后续如果上游或 runtime 有更准确 usage，可以在这里无缝替换。
    """

    stripped = str(text or "").strip()
    if not stripped:
        return 0

    cjk_tokens = 0
    ascii_chars = 0
    for char in stripped:
        codepoint = ord(char)
        if (
            0x3400 <= codepoint <= 0x4DBF
            or 0x4E00 <= codepoint <= 0x9FFF
            or 0xF900 <= codepoint <= 0xFAFF
        ):
            cjk_tokens += 1
        else:
            ascii_chars += 1
    return max(1, cjk_tokens + math.ceil(ascii_chars / 4))


def get_effective_context_window_tokens(model_metadata: Mapping[str, Any] | None = None) -> int:
    limits = dict(DEFAULT_MODEL_LIMITS)
    if isinstance(model_metadata, Mapping):
        limits.update(dict(model_metadata.get("limits") or {}))
        if model_metadata.get("context_window_tokens"):
            limits["context_window_tokens"] = model_metadata["context_window_tokens"]
        if model_metadata.get("max_output_tokens"):
            limits["max_output_tokens"] = model_metadata["max_output_tokens"]

    context_window = _coerce_positive_int(limits.get("context_window_tokens")) or DEFAULT_CONTEXT_WINDOW_TOKENS
    max_output_tokens = _coerce_positive_int(limits.get("max_output_tokens")) or DEFAULT_MAX_OUTPUT_TOKENS
    reserved_tokens = min(max_output_tokens, AUTOCOMPACT_SUMMARY_RESERVE_TOKENS)
    return max(1, context_window - reserved_tokens)


def get_auto_compact_threshold_tokens(model_metadata: Mapping[str, Any] | None = None) -> int:
    effective_context_window = get_effective_context_window_tokens(model_metadata)
    return max(1, effective_context_window - AUTOCOMPACT_BUFFER_TOKENS)


def get_auto_compact_threshold_percentage(model_metadata: Mapping[str, Any] | None = None) -> int:
    context_window = (
        _coerce_positive_int((model_metadata or {}).get("context_window_tokens"))
        if isinstance(model_metadata, Mapping)
        else None
    ) or DEFAULT_CONTEXT_WINDOW_TOKENS
    threshold_tokens = get_auto_compact_threshold_tokens(model_metadata)
    return max(0, min(100, int(round((threshold_tokens / context_window) * 100))))


def normalize_model_metadata(raw_model: Mapping[str, Any] | str | None) -> dict[str, Any]:
    """把模型目录统一规范成稳定 shape。

    设计目标：
    1. 未来上游 metadata 扩展时，这里只需要补 alias 映射。
    2. 当前前端/运行时只消费 canonical 字段，不直接依赖上游原始字段名。
    3. 原始 dict 字段尽量保留，避免把未来有用的信息在本地这一层再次裁掉。
    """

    base: dict[str, Any]
    if isinstance(raw_model, Mapping):
        base = dict(raw_model)
    else:
        base = {"id": str(raw_model or "unknown-model")}

    model_id = str(base.get("id") or base.get("name") or "unknown-model")
    display_name = str(base.get("display_name") or base.get("displayName") or model_id)

    context_window_tokens = (
        _coerce_token_limit(
            _lookup(
                base,
                "context_window_tokens",
                "metadata.context_window_tokens",
                "limits.context_window_tokens",
                "metadata.context_length",
                "metadata.context_window",
                "limits.context_length",
                "limits.context_window",
            )
        )
        or _coerce_token_limit(
            _lookup(
                base,
                "context_length",
                "context_window",
            ),
            assume_k_for_plain_values=True,
        )
        or DEFAULT_CONTEXT_WINDOW_TOKENS
    )
    max_output_tokens = (
        _coerce_token_limit(
            _lookup(
                base,
                "max_output_tokens",
                "metadata.max_output_tokens",
                "limits.max_output_tokens",
                "metadata.max_completion_tokens",
                "metadata.max_tokens",
                "limits.max_completion_tokens",
                "limits.max_tokens",
            )
        )
        or _coerce_token_limit(
            _lookup(
                base,
                "max_completion_tokens",
                "max_tokens",
            ),
            assume_k_for_plain_values=True,
        )
        or DEFAULT_MAX_OUTPUT_TOKENS
    )
    max_input_tokens = (
        _coerce_token_limit(
            _lookup(
                base,
                "max_input_tokens",
                "metadata.max_input_tokens",
                "limits.max_input_tokens",
            )
        )
        or _coerce_token_limit(
            _lookup(
                base,
                "input_max_length",
            ),
            assume_k_for_plain_values=True,
        )
        or DEFAULT_MAX_INPUT_TOKENS
    )
    max_reasoning_tokens = (
        _coerce_token_limit(
            _lookup(
                base,
                "max_reasoning_tokens",
                "metadata.max_reasoning_tokens",
                "limits.max_reasoning_tokens",
            )
        )
        or _coerce_token_limit(
            _lookup(
                base,
                "deep_thinking_max_length",
            ),
            assume_k_for_plain_values=True,
        )
        or DEFAULT_MAX_REASONING_TOKENS
    )
    rpm = (
        _coerce_positive_int(_lookup(base, "rpm", "metadata.rpm", "limits.rpm"))
        or DEFAULT_REQUESTS_PER_MINUTE
    )
    tpm = (
        _coerce_positive_int(_lookup(base, "tpm", "metadata.tpm", "limits.tpm"))
        or DEFAULT_TOKENS_PER_MINUTE
    )

    input_modalities = _extract_input_modalities(base)
    capabilities = {
        **DEFAULT_MODEL_CAPABILITIES,
        **dict(base.get("capabilities") or {}),
    }
    if input_modalities:
        capabilities["multimodal_input_image"] = "image" in input_modalities
        capabilities["multimodal_input_video"] = "video" in input_modalities
        capabilities["multimodal_input_file"] = "file" in input_modalities
    limits = {
        **DEFAULT_MODEL_LIMITS,
        **dict(base.get("limits") or {}),
        "context_window_tokens": context_window_tokens,
        "max_input_tokens": max_input_tokens,
        "max_output_tokens": max_output_tokens,
        "max_reasoning_tokens": max_reasoning_tokens,
        "rpm": rpm,
        "tpm": tpm,
    }
    pricing = {
        **DEFAULT_MODEL_PRICING,
        **dict(base.get("pricing") or {}),
    }

    normalized = {
        **base,
        "id": model_id,
        "display_name": display_name,
        "context_window_tokens": context_window_tokens,
        "max_output_tokens": max_output_tokens,
        "capabilities": capabilities,
        "limits": limits,
        "pricing": pricing,
    }
    normalized["auto_compact_threshold_tokens"] = get_auto_compact_threshold_tokens(normalized)
    normalized["auto_compact_threshold_percentage"] = get_auto_compact_threshold_percentage(normalized)
    return normalized

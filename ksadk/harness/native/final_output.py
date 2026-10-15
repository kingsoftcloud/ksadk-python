"""Conservative final-answer normalization for an explicit host output contract."""

from __future__ import annotations

from typing import Literal

from ksadk._model_json import load_model_json


def normalize_final_output(
    text: str, *, output_format: Literal["text", "json_object", "json_value"]
) -> str:
    """Unwrap one pure JSON fence; leave ambiguous or invalid output to verification.

    Text-mode answers never change. JSON-mode candidates must contain only the
    declared object or JSON value, optionally inside one complete unlabeled/json
    Markdown fence. The host never infers, repairs, or coerces field values.
    """
    if output_format == "text":
        return text
    candidate = text.strip()
    if candidate.startswith("```"):
        lines = candidate.splitlines()
        if len(lines) < 3 or lines[-1].strip() != "```":
            return text
        language = lines[0][3:].strip().lower()
        if language not in {"", "json"}:
            return text
        candidate = "\n".join(lines[1:-1]).strip()
    try:
        parsed = load_model_json(candidate)
    except (TypeError, ValueError, RecursionError):
        return text
    return candidate if output_format == "json_value" or isinstance(parsed, dict) else text

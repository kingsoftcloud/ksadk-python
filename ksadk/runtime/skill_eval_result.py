"""Opt-in Skill evaluation evidence, carried separately from assistant text."""

import json
import re
from collections.abc import Mapping
from typing import Any


def extract_skill_eval_result(value: Any) -> dict[str, Any]:
    """Read only an explicit state/result field; never parse model messages."""
    result = value.get("skill_eval_result") if isinstance(value, Mapping) else None
    if (
        not isinstance(result, Mapping)
        or result.get("schema_version") != "base_agent.skill_eval_result.v2"
    ):
        return {}
    try:
        return json.loads(json.dumps(dict(result), ensure_ascii=False, allow_nan=False))
    except (TypeError, ValueError, RecursionError):
        return {}


def skill_eval_response_fields(value: Any) -> dict[str, Any]:
    result = extract_skill_eval_result(value)
    if not result:
        return {}
    fields: dict[str, Any] = {"skill_eval_result": result}
    run = result.get("run")
    trace_id = run.get("trace_id") if isinstance(run, Mapping) else None
    if (
        isinstance(trace_id, str)
        and re.fullmatch(r"[0-9a-fA-F]{32}", trace_id)
        and int(trace_id, 16)
    ):
        fields["trace_id"] = trace_id.lower()
    return fields

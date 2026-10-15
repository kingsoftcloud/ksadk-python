"""Immutable provider input observations; no tool, replay or completion authority."""

import hashlib
import json
import re
from dataclasses import dataclass, field

from ksadk._model_json import load_model_json


def transformations_json(value: object) -> str:
    if not isinstance(value, list):
        raise ValueError("input transformations must be an array")
    for item in value:
        if not isinstance(item, dict) or type(item.get("type")) is not str:
            raise ValueError("input transformation requires a type")
        for key in ("path", "reason"):
            if item.get(key) is not None and type(item[key]) is not str:
                raise ValueError("input transformation optional field must be text")
    # Preserve unknown types, reasons and fields without interpreting them.
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def provider_prefix_digest(payload: dict) -> str:
    """Hash the actual outgoing request fields, not the API's per-block prefix.

    Credentials and generation options aren't included. This exact-body proof
    is deliberately not a reimplementation of the server's binding algorithm.
    """
    prefix = {key: payload[key] for key in ("system", "tools", "messages") if key in payload}
    return hashlib.sha256(
        json.dumps(
            prefix, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()


@dataclass(frozen=True)
class ProviderInputObservation:
    transformations_json: str = field(repr=False)
    provider_prefix_digest: str
    source: str

    def __post_init__(self):
        if type(self.transformations_json) is not str:
            raise ValueError("input observation requires immutable JSON")
        transformations_json(load_model_json(self.transformations_json))
        if (
            type(self.provider_prefix_digest) is not str
            or re.fullmatch(r"[0-9a-f]{64}", self.provider_prefix_digest) is None
        ):
            raise ValueError("input observation requires the actual request digest")
        if self.source not in {"buffered", "message_start", "message_delta"}:
            raise ValueError("input observation source is not a protocol boundary")


def input_observation(fields: dict, payload: dict, source: str) -> ProviderInputObservation | None:
    value = fields.get("input_transformations")
    if value is None:
        return None  # Absent/null is unknown; an explicit [] is a reported fact.
    return ProviderInputObservation(
        transformations_json(value), provider_prefix_digest(payload), source
    )


def transformation_summary(value: str) -> dict:
    counts = {}
    unknown = 0
    for item in load_model_json(value):
        kind, reason = item.get("type"), item.get("reason")
        if (
            kind == "thinking_dropped"
            and reason
            in {
                "prefix_binding_mismatch",
                "model_binding_mismatch",
                "organization_binding_mismatch",
            }
        ) or (kind == "thinking_mismatch_allowed" and reason == "prefix_binding_mismatch"):
            key = f"{kind}:{reason}"
            counts[key] = counts.get(key, 0) + 1
        else:
            unknown += 1
    return {"known_counts": counts, "unknown_count": unknown}

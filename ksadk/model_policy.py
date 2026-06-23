from __future__ import annotations

import copy
import json
import os
from typing import Any, Mapping

from ksadk.configs.settings import DEFAULT_MODEL_NAME

DEFAULT_MULTIMODAL_MODEL = "kimi-k2.7-code"
DEFAULT_FALLBACK_MODEL = "deepseek-v4-pro"

DEFAULT_MODEL_POLICY: dict[str, Any] = {
    "version": "v1",
    "primary": {"model": DEFAULT_MODEL_NAME},
    "multimodal": {"model": DEFAULT_MULTIMODAL_MODEL},
    "fallback": {
        "model": DEFAULT_FALLBACK_MODEL,
        "fallback_errors": [
            "timeout",
            "temporarily unavailable",
            "temporary unavailable",
            "model unavailable",
            "rate limit",
            "too many requests",
            "503",
            "504",
        ],
        "on_errors": [
            "timeout",
            "temporarily unavailable",
            "temporary unavailable",
            "model unavailable",
            "rate limit",
            "too many requests",
            "503",
            "504",
        ],
    },
    "models": {
        DEFAULT_MODEL_NAME: {
            "reasoning": True,
            "options": {},
        },
        DEFAULT_MULTIMODAL_MODEL: {
            "input": ["text", "image"],
            "reasoning": True,
            "options": {"temperature": 1},
        },
        DEFAULT_FALLBACK_MODEL: {
            "reasoning": True,
            "options": {},
        },
    },
}


def _deep_merge(base: dict[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    merged = copy.deepcopy(base)
    for key, value in override.items():
        if (
            isinstance(value, Mapping)
            and isinstance(merged.get(key), dict)
        ):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def normalize_model_policy(raw: str | Mapping[str, Any] | None = None) -> dict[str, Any]:
    if raw is None or raw == "":
        return copy.deepcopy(DEFAULT_MODEL_POLICY)
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return copy.deepcopy(DEFAULT_MODEL_POLICY)
    else:
        parsed = raw
    if not isinstance(parsed, Mapping):
        return copy.deepcopy(DEFAULT_MODEL_POLICY)
    normalized = _deep_merge(DEFAULT_MODEL_POLICY, parsed)
    fallback = normalized.get("fallback")
    if isinstance(fallback, dict):
        raw_errors = fallback.get("fallback_errors") or fallback.get("on_errors")
        if isinstance(raw_errors, list):
            fallback["fallback_errors"] = list(raw_errors)
            fallback["on_errors"] = list(raw_errors)
    return normalized


def _unqualified_model_name(model: str) -> str:
    return str(model or "").strip().rsplit("/", 1)[-1]


def model_policy_options_for_model(
    model: str,
    policy: str | Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    normalized = normalize_model_policy(policy)
    models = normalized.get("models")
    if not isinstance(models, Mapping):
        return {}

    candidates = [str(model or "").strip(), _unqualified_model_name(model)]
    for candidate in candidates:
        config = models.get(candidate)
        if isinstance(config, Mapping) and isinstance(config.get("options"), Mapping):
            return dict(config["options"])
    return {}


def fallback_model_for_exception(
    exc: BaseException,
    *,
    current_model: str,
    policy: str | Mapping[str, Any] | None = None,
) -> str | None:
    normalized = normalize_model_policy(policy)
    fallback = normalized.get("fallback")
    if not isinstance(fallback, Mapping):
        return None

    fallback_model = str(fallback.get("model") or "").strip()
    if not fallback_model or fallback_model == str(current_model or "").strip():
        return None

    message = str(exc or "").strip().lower()
    if not message:
        return None
    if "400" in message or "invalid request" in message or "bad request" in message:
        return None

    transient_markers = fallback.get("fallback_errors") or fallback.get("on_errors")
    if not isinstance(transient_markers, list):
        transient_markers = DEFAULT_MODEL_POLICY["fallback"]["on_errors"]

    if any(str(marker).lower() in message for marker in transient_markers):
        return fallback_model
    return None


def _provider_ref(model: str, provider: str = "ksyun") -> str:
    value = str(model or "").strip()
    if not value or "/" in value:
        return value
    return f"{provider}/{value}"


def _role_model(policy: Mapping[str, Any], role: str) -> str:
    value = policy.get(role)
    if isinstance(value, Mapping):
        return str(value.get("model") or "").strip()
    return str(value or "").strip()


def _catalog_from_policy(policy: Mapping[str, Any]) -> list[dict[str, Any]]:
    models = policy.get("models") if isinstance(policy.get("models"), Mapping) else {}
    catalog: list[dict[str, Any]] = []
    seen: set[str] = set()
    for role in ("primary", "multimodal", "fallback"):
        model = _role_model(policy, role)
        if not model or model in seen:
            continue
        seen.add(model)
        metadata = dict(models.get(model) or {}) if isinstance(models, Mapping) else {}
        item = {
            "id": model,
            "name": model,
            "api": metadata.get("api") or "openai-completions",
            "input": metadata.get("input") or ["text"],
        }
        if isinstance(metadata.get("reasoning"), bool):
            item["reasoning"] = metadata["reasoning"]
        if isinstance(metadata.get("options"), Mapping) and metadata["options"]:
            item["options"] = dict(metadata["options"])
        catalog.append(item)
    return catalog


def build_runtime_model_policy_env(
    env_vars: Mapping[str, str] | None,
    *,
    runtime: str,
    policy: str | Mapping[str, Any] | None = None,
) -> dict[str, str]:
    env = dict(env_vars or {})
    normalized = normalize_model_policy(policy or os.getenv("AGENTENGINE_MODEL_POLICY_JSON"))
    env.setdefault(
        "AGENTENGINE_MODEL_POLICY_JSON",
        json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
    )
    primary = _role_model(normalized, "primary")
    fallback = _role_model(normalized, "fallback")
    multimodal = _role_model(normalized, "multimodal")
    has_primary = any(
        str(env.get(key) or "").strip()
        for key in ("OPENCLAW_DEFAULT_MODEL", "HERMES_DEFAULT_MODEL", "OPENAI_MODEL_NAME", "MODEL_NAME")
    )
    runtime_name = str(runtime or "").strip().lower()
    if runtime_name == "openclaw":
        if primary and not has_primary:
            env["OPENAI_MODEL_NAME"] = _provider_ref(primary)
        if fallback:
            env.setdefault("OPENCLAW_FALLBACK_MODEL", _provider_ref(fallback))
        if multimodal:
            env.setdefault("OPENCLAW_IMAGE_MODEL", _provider_ref(multimodal))
        env.setdefault("OPENCLAW_MODEL_CATALOG_JSON", json.dumps(_catalog_from_policy(normalized), ensure_ascii=False))
        return env
    if runtime_name == "hermes":
        if primary and not has_primary:
            env["OPENAI_MODEL_NAME"] = primary
            env["HERMES_DEFAULT_MODEL"] = primary
        if fallback:
            env.setdefault("HERMES_FALLBACK_MODEL", fallback)
        env.setdefault("HERMES_MODEL_CATALOG_JSON", json.dumps(_catalog_from_policy(normalized), ensure_ascii=False))
        return env
    if primary and not has_primary:
        env["OPENAI_MODEL_NAME"] = primary
    if fallback:
        env.setdefault("OPENAI_FALLBACK_MODEL_NAME", fallback)
    return env

"""OpenAI-compatible model catalog helpers for product runtimes."""

from __future__ import annotations

from typing import Any, Mapping

import httpx

from ksadk.conversations.model_context import normalize_model_metadata


def _models_url(api_base: str) -> str:
    base = str(api_base or "").strip().rstrip("/")
    if base.endswith("/models"):
        return base
    if base.endswith("/v1"):
        return f"{base}/models"
    return f"{base}/v1/models"


def _extract_catalog_items(payload: Any) -> list[Any]:
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, Mapping):
        return []

    data = payload.get("data")
    if isinstance(data, list):
        return data
    if isinstance(data, Mapping):
        for key in ("models", "Models", "items", "Items", "data"):
            value = data.get(key)
            if isinstance(value, list):
                return value

    nested = payload.get("Data")
    if isinstance(nested, Mapping):
        for key in ("Models", "models", "Items", "items", "data"):
            value = nested.get(key)
            if isinstance(value, list):
                return value

    for key in ("models", "Models", "items", "Items"):
        value = payload.get(key)
        if isinstance(value, list):
            return value

    if payload.get("id") or payload.get("name"):
        return [payload]
    return []


def _model_identity_candidates(raw_model: Any) -> set[str]:
    if isinstance(raw_model, Mapping):
        values = [
            raw_model.get("id"),
            raw_model.get("name"),
            raw_model.get("model"),
            raw_model.get("display_name"),
            raw_model.get("displayName"),
        ]
    else:
        values = [raw_model]

    candidates: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        if not text:
            continue
        candidates.add(text)
        if "/" in text:
            candidates.add(text.split("/", 1)[1])
    return candidates


def find_model_in_catalog(raw_models: list[Any], model: str | None) -> Any | None:
    requested = _model_identity_candidates(model)
    if not requested:
        return None
    requested_lower = {item.lower() for item in requested}
    for raw_model in raw_models:
        identities = _model_identity_candidates(raw_model)
        if identities & requested:
            return raw_model
        if {item.lower() for item in identities} & requested_lower:
            return raw_model
    return None


async def fetch_provider_model_catalog(
    *,
    api_base: str | None,
    api_key: str | None = None,
    timeout: float = 10.0,
) -> list[dict[str, Any]]:
    """Fetch and normalize an OpenAI-compatible `/v1/models` catalog.

    The function intentionally treats provider failures as "no catalog" so deploy
    commands can keep working with explicit env/config fallback.
    """

    if not api_base:
        return []
    headers = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    try:
        async with httpx.AsyncClient(verify=False, timeout=timeout) as client:
            response = await client.get(_models_url(api_base), headers=headers)
            response.raise_for_status()
            payload = response.json()
    except Exception:
        return []

    normalized: list[dict[str, Any]] = []
    for raw_model in _extract_catalog_items(payload):
        item = normalize_model_metadata(raw_model)
        item["_provider_raw_model"] = raw_model if isinstance(raw_model, Mapping) else {"id": str(raw_model)}
        normalized.append(item)
    return normalized


async def fetch_provider_model_metadata(
    *,
    api_base: str | None,
    api_key: str | None,
    model: str | None,
    timeout: float = 10.0,
) -> dict[str, Any] | None:
    catalog = await fetch_provider_model_catalog(api_base=api_base, api_key=api_key, timeout=timeout)
    raw_models = [item.get("_provider_raw_model") or item for item in catalog]
    raw_match = find_model_in_catalog(raw_models, model)
    if raw_match is None:
        return None
    return normalize_model_metadata(raw_match)

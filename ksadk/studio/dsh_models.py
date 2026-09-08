"""Compose Core model routes from Studio's authoritative model resources.

Only references and provider metadata enter the temporary Cordis patch. Resolved
credentials live in the supervised process environment, never in DSH settings.
"""
from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field

from ksadk.studio.contracts import ModelSpec
from ksadk.studio.errors import StudioError


@dataclass
class DshStudioModels:
    providers: dict = field(default_factory=dict)
    environment: dict[str, str] = field(default_factory=dict, repr=False)
    default_provider: str | None = None
    default_model: str | None = None


def studio_model_projection(catalog, credentials) -> DshStudioModels:
    result = DshStudioModels()
    specs = []
    base = os.getenv("OPENAI_BASE_URL") or os.getenv("OPENAI_API_BASE")
    default = os.getenv("OPENAI_MODEL_NAME") or os.getenv("MODEL_NAME")
    if base and default:
        specs.append(ModelSpec(model=default, base_url=base, credential_ref="env://OPENAI_API_KEY"))
    for item in catalog.list(kind="model", limit=1000):
        specs.append(ModelSpec.model_validate(item.contract))
    for spec in specs:
        resolved = catalog.resolver.resolve_model(spec)
        try:
            key = credentials.resolve(resolved.credential_ref)
        except StudioError:
            # An unconfigured resource must not prevent other plugins starting.
            continue
        endpoint = resolved.endpoint_url
        suffix = "/responses" if resolved.wire_api == "responses" else "/chat/completions"
        if not endpoint.endswith(suffix):
            continue
        base_url = endpoint[:-len(suffix)]
        identity = f"{base_url}\0{resolved.credential_ref}\0{resolved.wire_api}"
        digest = hashlib.sha256(identity.encode()).hexdigest()[:12]
        route = f"studio-{digest}"
        reference = f"KSADK_STUDIO_MODEL_{digest.upper()}"
        result.environment[reference] = key
        provider = result.providers.setdefault(route, {
            "displayName": "Studio", "baseURL": base_url,
            "api": "openai-responses" if resolved.wire_api == "responses" else "openai-completions",
            "apiKeyEnv": reference, "models": [],
        })
        if not any(model["id"] == resolved.model for model in provider["models"]):
            provider["models"].append({"id": resolved.model, "name": resolved.model})
        if result.default_provider is None:
            result.default_provider, result.default_model = route, resolved.model
    return result

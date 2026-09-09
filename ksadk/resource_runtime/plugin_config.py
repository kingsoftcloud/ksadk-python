"""Data-only configuration contracts for the three official resource bundles.

These reserved plugin IDs map to scoped npm packages; recognizing an ID validates
configuration only. Installation identity and authorization still require Build
receipts and trusted activation admission.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from ksadk.resource_runtime.contracts import ResourceConfig, validate_resource_bindings

OFFICIAL_RESOURCE_PLUGINS = {
    "kingsoftcloud.dsh-knowledge": "knowledge-base",
    "kingsoftcloud.dsh-memory": "memory-instance",
    "kingsoftcloud.dsh-skill-center": "skill-space",
}


def resource_plugin_config(
    plugin_ref: str, ecosystem: str, config: Mapping[str, Any], *, enabled: bool
) -> ResourceConfig | None:
    plugin_id = plugin_ref.removeprefix("plugin://").rsplit("@", 1)[0]
    kind = OFFICIAL_RESOURCE_PLUGINS.get(plugin_id)
    if kind is None:
        return None
    if ecosystem != "dsh":
        raise ValueError("Official platform resource plugins require the DSH ecosystem")
    if not config and not enabled:
        return None
    parsed = ResourceConfig.model_validate(config)
    if parsed.binding.resource.kind != kind:
        raise ValueError("Resource kind does not match the official plugin")
    return parsed


def validate_resource_plugin_bindings(bindings: Sequence[Any]) -> None:
    resources = []
    for binding in bindings:
        if binding.enabled:
            resource = resource_plugin_config(
                binding.plugin_ref, binding.ecosystem, binding.config, enabled=True
            )
            if resource is not None:
                resources.append(resource)
    validate_resource_bindings(tuple(resources))

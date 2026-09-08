from __future__ import annotations

import pytest
import yaml
from pydantic import ValidationError

from ksadk.studio.codex_manifest import (
    CodexAgentManifest,
    CodexRuntimeRef,
    normalized_manifest_bytes,
)
from ksadk.studio.contracts import AgentBindings, NativePluginBinding

_SNAPSHOT = "sha256:" + "c" * 64


def _binding(**updates: object) -> NativePluginBinding:
    payload = {
        "ecosystem": "codex",
        "pluginRef": "plugin://io.codex.review@1.2.3",
        "snapshotDigest": _SNAPSHOT,
        "components": ["skill:review", "mcp:review-tools"],
    }
    payload.update(updates)
    return NativePluginBinding.model_validate(payload)


def test_codex_manifest_retains_immutable_native_plugin_selection() -> None:
    manifest = CodexAgentManifest(
        name="plugin-agent",
        version="1.0.0",
        runtime=CodexRuntimeRef(version="0.147.0"),
        model="test-model",
        prompt="Review the workspace.",
        plugins=[_binding()],
    )

    payload = yaml.safe_load(normalized_manifest_bytes(manifest))
    restored = CodexAgentManifest.model_validate(payload)
    assert restored.plugins == manifest.plugins
    assert restored.plugins is not None
    assert restored.plugins[0].snapshot_digest == _SNAPSHOT


def test_binding_rejects_clear_secret_material() -> None:
    with pytest.raises(ValidationError, match="Secret 引用|secret reference"):
        _binding(config={"apiToken": "clear-text"})


def test_agent_bindings_reject_duplicate_enabled_plugin_refs() -> None:
    with pytest.raises(ValidationError, match="不能重复"):
        AgentBindings(plugins=[_binding(), _binding(snapshotDigest="sha256:" + "d" * 64)])


def test_same_plugin_can_be_retained_disabled_during_migration() -> None:
    bindings = AgentBindings(
        plugins=[
            _binding(),
            _binding(snapshotDigest="sha256:" + "d" * 64, enabled=False),
        ]
    )
    assert [item.enabled for item in bindings.plugins] == [True, False]

"""Pure P2 composition resolution: no imports, process starts, or side effects."""
from __future__ import annotations

import pytest

from ksadk.plugins.contracts import CompositionProfile, PluginManifest
from ksadk.plugins.resolver import PluginRegistry, PluginResolutionError


def _manifest(
    plugin_id: str,
    *,
    definition: str,
    slot: str,
    mode: str = "unique",
    requires: list[dict] | None = None,
    digest_digit: str = "1",
) -> PluginManifest:
    return PluginManifest.model_validate(
        {
            "apiVersion": "plugin.ksadk.io/v1",
            "kind": "Plugin",
            "metadata": {"id": plugin_id, "version": "1.0.0"},
            "spec": {
                "domain": "ksadk-platform",
                "runtime": "python",
                "entrypoint": "tests.plugins:factory",
                "provides": [
                    {"definition": definition, "slot": slot, "mode": mode}
                ],
                "requires": requires or [],
                "isolation": "process",
                "compatibility": {"kernelApi": ">=1,<2"},
                "healthContract": "plugin.health/v1",
                "provenance": {
                    "source": "builtin",
                    "digest": f"sha256:{digest_digit * 64}",
                },
            },
        }
    )


def _profile(*, capabilities: list[dict] | None = None) -> CompositionProfile:
    return CompositionProfile.model_validate(
        {
            "apiVersion": "composition.ksadk.io/v1",
            "agentProvider": {"ref": "plugin://io.ksadk.provider@1.0.0"},
            "capabilities": capabilities or [],
        }
    )


def test_resolver_builds_deterministic_transitive_lock() -> None:
    provider = _manifest(
        "io.ksadk.provider",
        definition="agent.provider/v1",
        slot="agent.execution",
        requires=[{"definition": "session.event-store/v1", "version": ">=1,<2"}],
        digest_digit="1",
    )
    event_store = _manifest(
        "io.ksadk.event-store",
        definition="session.event-store/v1",
        slot="session.events",
        digest_digit="2",
    )

    first = PluginRegistry([provider, event_store]).resolve(_profile())
    second = PluginRegistry([event_store, provider]).resolve(_profile())

    assert first.plugin_lock == second.plugin_lock
    assert first.plugin_lock_digest == second.plugin_lock_digest
    assert [entry.id for entry in first.plugin_lock.plugins] == [
        "io.ksadk.event-store",
        "io.ksadk.provider",
    ]
    provider_entry = first.plugin_lock.plugins[1]
    assert provider_entry.dependencies[0].id == "io.ksadk.event-store"


def test_resolver_rejects_ambiguous_requirement() -> None:
    provider = _manifest(
        "io.ksadk.provider",
        definition="agent.provider/v1",
        slot="agent.execution",
        requires=[{"definition": "session.event-store/v1", "version": ">=1,<2"}],
    )
    one = _manifest(
        "io.ksadk.event-store-a",
        definition="session.event-store/v1",
        slot="session.events.a",
        digest_digit="2",
    )
    two = _manifest(
        "io.ksadk.event-store-b",
        definition="session.event-store/v1",
        slot="session.events.b",
        digest_digit="3",
    )

    with pytest.raises(PluginResolutionError, match="multiple plugins") as captured:
        PluginRegistry([provider, one, two]).resolve(_profile())
    assert captured.value.code == "plugin_requirement_ambiguous"


def test_resolver_rejects_second_unique_execution_owner() -> None:
    provider = _manifest(
        "io.ksadk.provider",
        definition="agent.provider/v1",
        slot="agent.execution",
    )
    conflicting = _manifest(
        "io.ksadk.another-provider",
        definition="agent.provider/v1",
        slot="agent.execution",
        digest_digit="4",
    )

    with pytest.raises(PluginResolutionError, match="unique slot") as captured:
        PluginRegistry([provider, conflicting]).resolve(
            _profile(capabilities=[{"ref": "plugin://io.ksadk.another-provider@1.0.0"}])
        )
    assert captured.value.code == "plugin_slot_conflict"


def test_resolver_never_executes_plugin_entrypoint() -> None:
    provider = _manifest(
        "io.ksadk.provider",
        definition="agent.provider/v1",
        slot="agent.execution",
    )
    resolved = PluginRegistry([provider]).resolve(_profile())
    assert resolved.profile.agent_provider.ref.endswith("@1.0.0")

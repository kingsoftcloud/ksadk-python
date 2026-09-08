from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from ksadk.plugins.contracts import (
    LockedPluginComponent,
    PluginLock,
    PluginLockEntry,
    PluginSourceSnapshot,
    canonical_plugin_lock,
)

_DIGEST_A = "sha256:" + "a" * 64
_DIGEST_B = "sha256:" + "b" * 64


def test_external_plugin_lock_retains_exact_source_and_component_bytes() -> None:
    lock = PluginLock(
        plugins=[
            PluginLockEntry(
                id="io.codex.example",
                version="1.2.3",
                digest=_DIGEST_A,
                source="market",
                upstream=PluginSourceSnapshot(
                    ecosystem="codex",
                    type="git",
                    requested="https://github.com/example/plugins.git",
                    resolved="https://github.com/example/plugins.git@0123456789abcdef",
                    marketplace="example-marketplace",
                ),
                components=(
                    LockedPluginComponent(
                        id="example:review",
                        kind="skill",
                        digest=_DIGEST_B,
                        path="skills/review/SKILL.md",
                    ),
                ),
            )
        ]
    )

    payload = json.loads(canonical_plugin_lock(lock))
    entry = payload["plugins"][0]
    assert entry["upstream"]["resolved"].endswith("@0123456789abcdef")
    assert entry["components"] == [
        {
            "id": "example:review",
            "kind": "skill",
            "digest": _DIGEST_B,
            "path": "skills/review/SKILL.md",
        }
    ]


def test_legacy_lock_wire_shape_does_not_gain_empty_snapshot_fields() -> None:
    lock = PluginLock(
        plugins=[
            PluginLockEntry(
                id="io.ksadk.legacy",
                version="1.0.0",
                digest=_DIGEST_A,
                source="builtin",
            )
        ]
    )

    payload = json.loads(canonical_plugin_lock(lock))["plugins"][0]
    assert "upstream" not in payload
    assert "components" not in payload


def test_component_order_is_canonical_for_lock_digest_stability() -> None:
    components = (
        LockedPluginComponent(id="mcp:z", kind="mcp", digest=_DIGEST_A),
        LockedPluginComponent(id="skill:a", kind="skill", digest=_DIGEST_B),
    )
    forward = PluginLockEntry(
        id="io.codex.example",
        version="1.2.3",
        digest=_DIGEST_A,
        source="market",
        components=components,
    )
    reverse = PluginLockEntry(
        id="io.codex.example",
        version="1.2.3",
        digest=_DIGEST_A,
        source="market",
        components=tuple(reversed(components)),
    )

    assert forward == reverse


@pytest.mark.parametrize(
    "field,value",
    [
        ("requested", "https://user:token@example.com/plugins.git"),
        ("resolved", "https://example.com/plugins.git?token=secret"),
        ("registry", "https://registry.example.com/#credential"),
    ],
)
def test_source_snapshot_rejects_credential_bearing_coordinates(field: str, value: str) -> None:
    payload = {
        "ecosystem": "codex",
        "type": "git",
        "requested": "https://example.com/plugins.git",
        "resolved": "https://example.com/plugins.git@deadbeef",
        field: value,
    }
    with pytest.raises(ValidationError, match="must not contain credentials"):
        PluginSourceSnapshot.model_validate(payload)


@pytest.mark.parametrize("path", ["../SKILL.md", "/tmp/SKILL.md", "skills\\x\\SKILL.md"])
def test_component_snapshot_rejects_escaping_paths(path: str) -> None:
    with pytest.raises(ValidationError, match="path"):
        LockedPluginComponent(
            id="example:review",
            kind="skill",
            digest=_DIGEST_A,
            path=path,
        )


def test_lock_rejects_inconsistent_native_and_upstream_sources() -> None:
    with pytest.raises(ValidationError, match="inconsistent"):
        PluginLockEntry(
            id="io.codex.example",
            version="1.2.3",
            digest=_DIGEST_A,
            source="builtin",
            upstream=PluginSourceSnapshot(
                ecosystem="codex",
                type="git",
                requested="https://github.com/example/plugins.git",
                resolved="https://github.com/example/plugins.git@deadbeef",
            ),
        )

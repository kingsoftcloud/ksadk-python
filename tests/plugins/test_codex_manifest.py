from __future__ import annotations

import json
from pathlib import Path

import pytest

from ksadk.plugins.codex_manifest import (
    CodexPluginManifestError,
    CodexPluginSourceCoordinate,
    load_codex_plugin_manifest,
    snapshot_installed_codex_plugin,
)
from ksadk.plugins.ecosystem_probe import EcosystemProbeError, probe_ecosystem_manifests


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _plugin_root(tmp_path: Path, *, snake_case: bool = False) -> Path:
    root = tmp_path / "plugin"
    (root / "skills" / "review").mkdir(parents=True)
    (root / "skills" / "review" / "SKILL.md").write_text(
        "---\nname: review\ndescription: Review a fixture.\n---\n\n# Review\n",
        encoding="utf-8",
    )
    (root / "assets").mkdir()
    (root / "assets" / "icon.svg").write_text("<svg/>\n", encoding="utf-8")
    _write_json(
        root / ".mcp.json",
        {
            "mcp_servers" if snake_case else "mcpServers": {
                "counter": {
                    "command": "python3",
                    "args": ["./scripts/counter.py"],
                }
            }
        },
    )
    _write_json(
        root / "hooks.json",
        {
            "hooks": {
                "SessionStart": [
                    {"hooks": [{"type": "command", "command": "./scripts/session-start.sh"}]}
                ]
            }
        },
    )
    _write_json(
        root / ".app.json",
        {"apps": {"fixture": {"id": "connector_fixture", "required": True}}},
    )
    manifest: dict[str, object] = {
        "name": "fixture-plugin",
        "version": "1.2.3",
        "description": "Full Codex plugin fixture.",
        "author": {"name": "KsADK"},
        "skills": "./skills/",
        "hooks": "./hooks.json",
        "apps": "./.app.json",
        "futureNativeField": {"preserved": True},
    }
    if snake_case:
        manifest["mcp_servers"] = "./.mcp.json"
        manifest["interface"] = {
            "display_name": "Fixture",
            "default_prompt": "Use the fixture.",
            "composer_icon": "./assets/icon.svg",
        }
        manifest["bundled_content_variant"] = "test"
    else:
        manifest["mcpServers"] = "./.mcp.json"
        manifest["interface"] = {
            "displayName": "Fixture",
            "defaultPrompt": ["Use the fixture."],
            "composerIcon": "./assets/icon.svg",
        }
        manifest["bundledContentVariant"] = "test"
    _write_json(root / ".codex-plugin" / "plugin.json", manifest)
    return root


@pytest.mark.parametrize("snake_case", [False, True])
def test_manifest_accepts_camel_and_snake_forms_and_snapshots_components(
    tmp_path: Path,
    snake_case: bool,
) -> None:
    root = _plugin_root(tmp_path, snake_case=snake_case)

    manifest = load_codex_plugin_manifest(root)
    snapshot = snapshot_installed_codex_plugin(
        root,
        source=CodexPluginSourceCoordinate(
            type="local",
            requested=root.resolve().as_uri(),
            resolved=root.resolve().as_uri(),
            marketplace_name="fixture-marketplace",
        ),
    )

    assert manifest.name == "fixture-plugin"
    assert manifest.mcp_servers == "./.mcp.json"
    assert manifest.interface is not None
    assert manifest.interface.display_name == "Fixture"
    assert manifest.bundled_content_variant == "test"
    assert manifest.model_extra == {"futureNativeField": {"preserved": True}}
    assert snapshot.installed_root == str(root.resolve())
    assert snapshot.manifest == manifest
    assert snapshot.manifest_digest.startswith("sha256:")
    assert snapshot.artifact_digest.startswith("sha256:")
    assert {(item.kind, item.name, item.path) for item in snapshot.components} == {
        ("skill", "review", "skills/review"),
        ("mcp", "counter", ".mcp.json"),
        ("hook", "SessionStart", "hooks.json"),
        ("app", "fixture", ".app.json"),
    }
    assert snapshot.source.to_plugin_source_snapshot().ecosystem == "codex"
    assert {item.kind for item in (part.to_locked_component() for part in snapshot.components)} == {
        "skill",
        "mcp",
        "hook",
        "app",
    }


def test_snapshot_is_deterministic_and_changes_with_component_bytes(tmp_path: Path) -> None:
    root = _plugin_root(tmp_path)
    source = CodexPluginSourceCoordinate(
        type="local",
        requested=root.resolve().as_uri(),
        resolved=root.resolve().as_uri(),
    )

    before = snapshot_installed_codex_plugin(root, source=source)
    repeated = snapshot_installed_codex_plugin(root, source=source)
    assert repeated == before

    skill_path = root / "skills" / "review" / "SKILL.md"
    skill_path.write_text(skill_path.read_text(encoding="utf-8") + "Changed.\n", encoding="utf-8")
    after = snapshot_installed_codex_plugin(root, source=source)

    assert after.manifest_digest == before.manifest_digest
    assert after.artifact_digest != before.artifact_digest
    assert (
        next(item for item in after.components if item.kind == "skill").content_digest
        != next(item for item in before.components if item.kind == "skill").content_digest
    )


@pytest.mark.parametrize(
    ("camel", "snake"),
    [
        ("mcpServers", "mcp_servers"),
        ("bundledContentVariant", "bundled_content_variant"),
    ],
)
def test_manifest_rejects_alias_collisions(
    tmp_path: Path,
    camel: str,
    snake: str,
) -> None:
    root = _plugin_root(tmp_path)
    manifest_path = root / ".codex-plugin" / "plugin.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest[snake] = manifest[camel]
    _write_json(manifest_path, manifest)

    with pytest.raises(CodexPluginManifestError, match="cannot contain both"):
        load_codex_plugin_manifest(root)


def test_manifest_rejects_nested_interface_alias_collision(tmp_path: Path) -> None:
    root = _plugin_root(tmp_path)
    manifest_path = root / ".codex-plugin" / "plugin.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["interface"]["default_prompt"] = "collision"
    _write_json(manifest_path, manifest)

    with pytest.raises(CodexPluginManifestError, match="cannot contain both"):
        load_codex_plugin_manifest(root)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("name", 7, "name"),
        ("version", "latest", "semantic version"),
        ("keywords", "not-an-array", "keywords"),
        ("mcpServers", [], "mcpServers"),
        ("author", "KsADK", "author"),
        ("interface", [], "interface"),
    ],
)
def test_manifest_rejects_wrong_known_field_types(
    tmp_path: Path,
    field: str,
    value: object,
    message: str,
) -> None:
    root = _plugin_root(tmp_path)
    manifest_path = root / ".codex-plugin" / "plugin.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest[field] = value
    _write_json(manifest_path, manifest)

    with pytest.raises(CodexPluginManifestError, match=message):
        load_codex_plugin_manifest(root)


@pytest.mark.parametrize("field", ["skills", "hooks", "apps", "mcpServers"])
def test_manifest_rejects_paths_that_escape_installed_root(tmp_path: Path, field: str) -> None:
    root = _plugin_root(tmp_path)
    manifest_path = root / ".codex-plugin" / "plugin.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest[field] = "../outside"
    _write_json(manifest_path, manifest)

    with pytest.raises(CodexPluginManifestError, match="relative to the plugin root"):
        load_codex_plugin_manifest(root)


def test_manifest_rejects_component_symlink(tmp_path: Path) -> None:
    root = _plugin_root(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "SKILL.md").write_text("unsafe\n", encoding="utf-8")
    link = root / "linked-skill"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks are unavailable on this platform")
    manifest_path = root / ".codex-plugin" / "plugin.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["skills"] = "./linked-skill"
    _write_json(manifest_path, manifest)

    with pytest.raises(CodexPluginManifestError, match="symlink"):
        load_codex_plugin_manifest(root)


@pytest.mark.parametrize(
    ("relative_path", "payload", "message"),
    [
        (".mcp.json", {"mcpServers": {"counter": "not-an-object"}}, "must be an object"),
        ("hooks.json", {"hooks": {"SessionStart": {}}}, "array of objects"),
        (".app.json", {"apps": {"fixture": "not-an-object"}}, "must be an object"),
    ],
)
def test_manifest_rejects_invalid_companion_shapes(
    tmp_path: Path,
    relative_path: str,
    payload: object,
    message: str,
) -> None:
    root = _plugin_root(tmp_path)
    _write_json(root / relative_path, payload)

    with pytest.raises(CodexPluginManifestError, match=message):
        load_codex_plugin_manifest(root)


def test_probe_uses_full_codex_manifest_validation(tmp_path: Path) -> None:
    root = _plugin_root(tmp_path)
    manifest_path = root / ".codex-plugin" / "plugin.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["mcpServers"] = "../outside.json"
    _write_json(manifest_path, manifest)

    with pytest.raises(EcosystemProbeError, match="relative to the plugin root"):
        probe_ecosystem_manifests(root)


def test_checked_in_real_marketplace_fixture_is_parseable() -> None:
    root = (
        Path(__file__).parents[1]
        / "e2e"
        / "fixtures"
        / "codex-marketplace"
        / "plugins"
        / "ksadk-bridge-e2e"
    )

    manifest = load_codex_plugin_manifest(root)
    assert manifest.name == "ksadk-bridge-e2e"
    assert manifest.version == "0.1.0"
    marketplace = root.parents[1] / ".agents" / "plugins" / "marketplace.json"
    payload = json.loads(marketplace.read_text(encoding="utf-8"))
    assert payload["plugins"][0]["source"]["path"] == "./plugins/ksadk-bridge-e2e"


def test_only_official_default_hook_path_is_discovered_when_fields_are_absent(
    tmp_path: Path,
) -> None:
    root = _plugin_root(tmp_path)
    manifest_path = root / ".codex-plugin" / "plugin.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for field in ("skills", "mcpServers", "hooks", "apps"):
        manifest.pop(field, None)
    _write_json(manifest_path, manifest)
    default_hook = root / "hooks" / "hooks.json"
    default_hook.parent.mkdir()
    _write_json(
        default_hook,
        {
            "description": "Official default plugin hook location.",
            "hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "true"}]}]},
        },
    )

    snapshot = snapshot_installed_codex_plugin(
        root,
        source=CodexPluginSourceCoordinate(
            type="local", requested=str(root), resolved=str(root)
        ),
    )

    assert [(item.kind, item.name, item.path) for item in snapshot.components] == [
        ("hook", "SessionStart", "hooks/hooks.json")
    ]


def test_manifest_supports_multiple_declared_skill_paths(tmp_path: Path) -> None:
    root = _plugin_root(tmp_path)
    second = root / "other-skills" / "summarize" / "SKILL.md"
    second.parent.mkdir(parents=True)
    second.write_text(
        "---\nname: summarize\ndescription: Summarize fixture.\n---\n",
        encoding="utf-8",
    )
    manifest_path = root / ".codex-plugin" / "plugin.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["skills"] = ["./skills/", "./other-skills/"]
    _write_json(manifest_path, manifest)

    snapshot = snapshot_installed_codex_plugin(
        root,
        source=CodexPluginSourceCoordinate(
            type="local", requested=str(root), resolved=str(root)
        ),
    )

    assert {item.name for item in snapshot.components if item.kind == "skill"} == {
        "review",
        "summarize",
    }


@pytest.mark.parametrize(
    ("declaration", "expected_events"),
    [
        ("./hooks.json", {"SessionStart"}),
        (["./hooks.json", "./second-hooks.json"], {"SessionStart", "Stop"}),
        (
            {
                "description": "inline",
                "hooks": {
                    "UserPromptSubmit": [
                        {"hooks": [{"type": "command", "command": "true"}]}
                    ]
                },
            },
            {"UserPromptSubmit"},
        ),
        (
            [
                {
                    "hooks": {
                        "SessionStart": [
                            {"hooks": [{"type": "command", "command": "true"}]}
                        ]
                    }
                },
                {
                    "hooks": {
                        "Stop": [{"hooks": [{"type": "command", "command": "true"}]}]
                    }
                },
            ],
            {"SessionStart", "Stop"},
        ),
    ],
)
def test_manifest_accepts_all_official_hook_declaration_forms(
    tmp_path: Path,
    declaration: object,
    expected_events: set[str],
) -> None:
    root = _plugin_root(tmp_path)
    _write_json(
        root / "second-hooks.json",
        {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "true"}]}]}},
    )
    manifest_path = root / ".codex-plugin" / "plugin.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["hooks"] = declaration
    _write_json(manifest_path, manifest)

    snapshot = snapshot_installed_codex_plugin(
        root,
        source=CodexPluginSourceCoordinate(
            type="local", requested=str(root), resolved=str(root)
        ),
    )

    assert {item.name for item in snapshot.components if item.kind == "hook"} == expected_events


def test_manifest_requires_dot_prefixed_explicit_component_paths(tmp_path: Path) -> None:
    root = _plugin_root(tmp_path)
    manifest_path = root / ".codex-plugin" / "plugin.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["skills"] = "skills"
    _write_json(manifest_path, manifest)

    with pytest.raises(CodexPluginManifestError, match="must start with './'"):
        load_codex_plugin_manifest(root)

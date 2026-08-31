"""Phase 2 v1 contracts may grow additively, but cannot change old wire shapes."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
BASELINE_ROOT = ROOT / "tests" / "contracts" / "baselines" / "phase2"
CONTRACT_ROOT = ROOT / "contracts"
CONTRACT_SETS = {
    "plugin": CONTRACT_ROOT / "plugin" / "v1",
    "conversation": CONTRACT_ROOT / "conversation" / "v1",
    "scheduler": CONTRACT_ROOT / "scheduler" / "v1",
}

# Constraints on an existing value cannot be introduced, removed, or changed in
# the same v1. Descriptive metadata is intentionally excluded.
STABLE_CONSTRAINTS = {
    "$ref",
    "type",
    "const",
    "enum",
    "format",
    "pattern",
    "minLength",
    "maxLength",
    "minimum",
    "maximum",
    "exclusiveMinimum",
    "exclusiveMaximum",
    "multipleOf",
    "minItems",
    "maxItems",
    "uniqueItems",
    "minProperties",
    "maxProperties",
    "additionalProperties",
    "unevaluatedProperties",
    "contentEncoding",
    "contentMediaType",
}
NAMED_SCHEMA_MAPS = {"properties", "$defs", "definitions", "patternProperties"}
SINGLE_SCHEMAS = {"items", "contains", "not", "if", "then", "else", "propertyNames"}
SCHEMA_LISTS = {"allOf", "anyOf", "oneOf", "prefixItems"}


def _load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def assert_additive_schema(old: Any, new: Any, *, path: str = "$") -> None:
    """Reject changes that can invalidate or reinterpret a frozen v1 payload."""

    if isinstance(old, bool) or isinstance(new, bool):
        assert old == new, f"{path}: boolean schema changed"
        return
    assert isinstance(old, dict) and isinstance(new, dict), f"{path}: schema kind changed"

    assert set(old.get("required", [])) == set(new.get("required", [])), (
        f"{path}: required fields changed"
    )
    for key in STABLE_CONSTRAINTS:
        assert old.get(key) == new.get(key), f"{path}: {key} changed"

    for key in NAMED_SCHEMA_MAPS:
        old_entries = old.get(key, {})
        new_entries = new.get(key, {})
        assert isinstance(old_entries, dict) and isinstance(new_entries, dict), (
            f"{path}/{key}: schema map changed kind"
        )
        assert set(old_entries) <= set(new_entries), f"{path}/{key}: entry removed"
        for name, old_entry in old_entries.items():
            assert_additive_schema(
                old_entry,
                new_entries[name],
                path=f"{path}/{key}/{name}",
            )

    for key in SINGLE_SCHEMAS:
        assert (key in old) == (key in new), f"{path}/{key}: constraint presence changed"
        if key in old:
            assert_additive_schema(old[key], new[key], path=f"{path}/{key}")

    for key in SCHEMA_LISTS:
        old_entries = old.get(key)
        new_entries = new.get(key)
        assert (old_entries is None) == (new_entries is None), f"{path}/{key}: alternatives changed"
        if old_entries is None:
            continue
        assert isinstance(old_entries, list) and isinstance(new_entries, list)
        assert len(old_entries) == len(new_entries), f"{path}/{key}: alternatives changed"
        for index, old_entry in enumerate(old_entries):
            assert_additive_schema(
                old_entry,
                new_entries[index],
                path=f"{path}/{key}/{index}",
            )


@pytest.mark.parametrize("contract_set", sorted(CONTRACT_SETS))
def test_phase2_v1_schemas_are_additive_against_frozen_baseline(
    contract_set: str,
) -> None:
    current_dir = CONTRACT_SETS[contract_set]
    baseline_dir = BASELINE_ROOT / contract_set
    baselines = sorted(baseline_dir.glob("*.schema.json"))
    assert baselines, f"missing frozen baseline for {contract_set}"
    assert {path.name for path in baselines} == {
        path.name for path in current_dir.glob("*.schema.json")
    }
    for baseline in baselines:
        assert_additive_schema(
            _load(baseline),
            _load(current_dir / baseline.name),
            path=f"{contract_set}/{baseline.name}",
        )


def test_phase2_schema_artifact_inventory_excludes_unpublished_package_formats() -> None:
    inventory = {
        contract_set: sorted(path.name for path in directory.glob("*.schema.json"))
        for contract_set, directory in CONTRACT_SETS.items()
    }
    assert {name: len(paths) for name, paths in inventory.items()} == {
        "plugin": 9,
        "conversation": 3,
        "scheduler": 2,
    }
    # Some source contracts (for example EcosystemPluginDescriptor and its
    # receipt) intentionally share one schema artifact.  This counts files,
    # not the number of frozen public models.
    assert sum(map(len, inventory.values())) == 14


def test_unpublished_plugin_package_schema_is_absent_from_current_and_baseline() -> None:
    """P2-00 must not freeze the removed Python/KsADK package experiment."""

    forbidden_names = {
        "ksadk-plugin.schema.json",
        "plugin-manifest.schema.json",
        "python-plugin.schema.json",
    }
    for root in (CONTRACT_SETS["plugin"], BASELINE_ROOT / "plugin"):
        names = {path.name for path in root.glob("*.schema.json")}
        assert names.isdisjoint(forbidden_names)
        for path in root.glob("*.schema.json"):
            schema = _load(path)
            assert schema.get("title") != "PluginManifest/v1"
            kind = schema.get("properties", {}).get("kind", {})
            assert kind.get("const") != "Plugin"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda schema: schema["required"].append("newRequired"),
        lambda schema: schema["properties"].pop("stable"),
        lambda schema: schema["properties"]["stable"].update({"maxLength": 8}),
        lambda schema: schema["properties"]["stable"].update({"enum": ["other"]}),
    ],
)
def test_additive_gate_rejects_representative_breaking_changes(mutate) -> None:
    old = {
        "type": "object",
        "required": ["stable"],
        "properties": {"stable": {"type": "string"}},
        "additionalProperties": False,
    }
    new = json.loads(json.dumps(old))
    mutate(new)
    with pytest.raises(AssertionError):
        assert_additive_schema(old, new)


def test_additive_gate_allows_new_optional_property_and_definition() -> None:
    old = {
        "type": "object",
        "required": ["stable"],
        "properties": {"stable": {"type": "string"}},
        "$defs": {"stable": {"type": "string"}},
        "additionalProperties": False,
    }
    new = json.loads(json.dumps(old))
    new["properties"]["optional"] = {"type": "number"}
    new["$defs"]["optional"] = {"type": "number"}
    assert_additive_schema(old, new)

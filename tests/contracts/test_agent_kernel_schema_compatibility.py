# Agent Kernel v1 additive-only breaking-change gate。
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

CONTRACT_DIR = Path(__file__).resolve().parents[2] / "contracts" / "agent-kernel" / "v1"
SCHEMA_FILES = [
    "agent-control.schema.json",
    "session-event.schema.json",
    "activation-lease.schema.json",
    "runtime-capability.schema.json",
]


def canonical_bytes(data) -> bytes:
    return json.dumps(data, sort_keys=True, separators=(",", ":")).encode()


def test_manifest_exists_and_lists_all_contract_files():
    manifest = json.loads((CONTRACT_DIR / "manifest.json").read_text())
    assert manifest["contract_set"] == "agent-kernel/v1"
    listed = {entry["path"] for entry in manifest["files"]}
    for name in SCHEMA_FILES:
        assert name in listed
    aggregate = hashlib.sha256()
    for path in sorted(p for p in CONTRACT_DIR.rglob("*") if p.is_file() and p.name != "manifest.json"):
        canonical = canonical_bytes(json.loads(path.read_text()))
        rel = path.relative_to(CONTRACT_DIR).as_posix()
        assert rel in listed
        assert dict((e["path"], e) for e in manifest["files"])[rel]["sha256"] == hashlib.sha256(canonical).hexdigest()
        aggregate.update(rel.encode() + b"\0" + canonical)
    assert manifest["aggregate_digest"] == aggregate.hexdigest()
    assert len(manifest["aggregate_digest"]) == 64


def assert_additive_only(old: dict, new: dict) -> None:
    assert set(old.get("required", [])) == set(new.get("required", []))
    old_props = old.get("properties", {})
    new_props = new.get("properties", {})
    assert set(old_props) <= set(new_props)
    for name, schema in old_props.items():
        if not isinstance(schema, dict):
            continue  # boolean schema（如 true）无 type 约束可收窄
        assert new_props[name].get("type") == schema.get("type")


@pytest.mark.parametrize("name", SCHEMA_FILES)
def test_schema_is_draft_2020_12(name):
    schema = json.loads((CONTRACT_DIR / name).read_text())
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"


@pytest.mark.parametrize("name", SCHEMA_FILES)
def test_schema_additive_only_against_manifest_baseline(name):
    # manifest digest 与磁盘上 schema 一致；schema 未来只能做 additive 变更，
    # 改动后必须重新生成 manifest 并由架构 Owner review。
    manifest = json.loads((CONTRACT_DIR / "manifest.json").read_text())
    entries = {e["path"]: e for e in manifest["files"]}
    raw = (CONTRACT_DIR / name).read_bytes()
    schema = json.loads(raw)
    assert entries[name]["sha256"] == hashlib.sha256(canonical_bytes(schema)).hexdigest()
    # 自检：当前 schema 满足 additive-only 规则。
    assert_additive_only(schema, schema)


def _walk_defs(schema, prefix=""):
    if not isinstance(schema, dict):
        return
    yield prefix or "$", schema
    for key, sub in schema.get("$defs", {}).items():
        yield from _walk_defs(sub, f"$defs/{key}")


def test_schema_defs_are_additive_only_self_compatible():
    for name in SCHEMA_FILES:
        schema = json.loads((CONTRACT_DIR / name).read_text())
        for def_path, node in _walk_defs(schema):
            if node.get("type") == "object":
                assert_additive_only(node, node)


def test_every_fixture_validates_against_its_schema():
    import jsonschema

    validator_kwargs = {"format_checker": jsonschema.FormatChecker()}
    schemas = {name: json.loads((CONTRACT_DIR / name).read_text()) for name in SCHEMA_FILES}
    fixtures_dir = CONTRACT_DIR / "fixtures"
    checked = 0
    for path in sorted(fixtures_dir.glob("*.json")):
        data = json.loads(path.read_text())
        items = data if isinstance(data, list) else [data]
        matches = _matching_schemas(path.name, data, schemas)
        assert matches, f"fixture {path.name} 未关联任何 schema"
        for schema_name in matches:
            validator = jsonschema.Draft202012Validator(schemas[schema_name], **validator_kwargs)
            for item in items:
                validator.validate(item)
                checked += 1
    assert checked >= 10


def _matching_schemas(fixture_name: str, data, schemas) -> list[str]:
    if fixture_name.startswith("agent-control"):
        return ["agent-control.schema.json"]
    if fixture_name.startswith("session-event"):
        return ["session-event.schema.json"]
    if fixture_name.startswith("activation-lease"):
        return ["activation-lease.schema.json"]
    if fixture_name.startswith("runtime-capability"):
        return ["runtime-capability.schema.json"]
    if fixture_name.startswith("agent-status"):
        return ["agent-control.schema.json"]
    return []

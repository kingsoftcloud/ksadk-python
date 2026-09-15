"""Conversation v1 source, JSON Schema, fixture and digest gates."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from ksadk.conversations.contracts import (
    ConversationInput,
    ConversationItem,
    ConversationSurface,
)

CONTRACT_DIR = Path(__file__).resolve().parents[2] / "contracts" / "conversation" / "v1"
CASES = (
    ("conversation-input.schema.json", "conversation-input.json", ConversationInput),
    ("conversation-item.schema.json", "conversation-item.json", ConversationItem),
    ("conversation-surface.schema.json", "conversation-surface.json", ConversationSurface),
)


def _json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize(("schema_name", "fixture_name", "model"), CASES)
def test_conversation_fixtures_match_schema_and_source_type(
    schema_name: str,
    fixture_name: str,
    model: type,
) -> None:
    fixture = _json(CONTRACT_DIR / "fixtures" / fixture_name)
    Draft202012Validator(_json(CONTRACT_DIR / schema_name)).validate(fixture)
    assert model.model_validate(fixture).model_dump(
        by_alias=True,
        exclude_none=True,
        mode="json",
    )


def test_conversation_manifest_locks_every_schema_and_fixture() -> None:
    manifest = _json(CONTRACT_DIR / "manifest.json")
    assert manifest["contract_set"] == "conversation/v1"
    entries = {entry["path"]: entry for entry in manifest["files"]}
    aggregate = hashlib.sha256()
    discovered: set[str] = set()
    for path in sorted(CONTRACT_DIR.rglob("*.json")):
        if path.name == "manifest.json":
            continue
        canonical = json.dumps(
            _json(path),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        relative = path.relative_to(CONTRACT_DIR).as_posix()
        discovered.add(relative)
        assert entries[relative]["sha256"] == hashlib.sha256(canonical).hexdigest()
        assert entries[relative]["bytes"] == len(canonical)
        aggregate.update(relative.encode("utf-8") + b"\0" + canonical)
    assert set(entries) == discovered
    assert manifest["aggregate_digest"] == aggregate.hexdigest()

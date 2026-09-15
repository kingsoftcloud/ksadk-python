"""Scheduler v1 source, JSON Schema and digest regression tests."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from ksadk.scheduler.contracts import ScheduledTask, ScheduleOccurrence

CONTRACT_DIR = Path(__file__).resolve().parents[2] / "contracts" / "scheduler" / "v1"
CASES = (
    ("scheduled-task.schema.json", "scheduled-task.json", ScheduledTask),
    ("schedule-occurrence.schema.json", "schedule-occurrence.json", ScheduleOccurrence),
)


def _json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize(("schema_name", "fixture_name", "model"), CASES)
def test_scheduler_fixtures_match_schema_and_source_type(
    schema_name: str,
    fixture_name: str,
    model: type,
) -> None:
    payload = _json(CONTRACT_DIR / "fixtures" / fixture_name)
    Draft202012Validator(_json(CONTRACT_DIR / schema_name)).validate(payload)
    assert model.model_validate(payload).model_dump(by_alias=True, mode="json")


def test_scheduler_contract_manifest_locks_every_schema_and_fixture() -> None:
    manifest = _json(CONTRACT_DIR / "manifest.json")
    assert manifest["contract_set"] == "scheduler/v1"
    entries = {entry["path"]: entry for entry in manifest["files"]}
    aggregate = hashlib.sha256()
    for path in sorted(CONTRACT_DIR.rglob("*.json")):
        if path.name == "manifest.json":
            continue
        canonical = json.dumps(
            _json(path), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        relative = path.relative_to(CONTRACT_DIR).as_posix()
        assert entries[relative]["sha256"] == hashlib.sha256(canonical).hexdigest()
        aggregate.update(relative.encode("utf-8") + b"\0" + canonical)
    assert manifest["aggregate_digest"] == aggregate.hexdigest()


def test_schedule_contract_rejects_clear_secrets_and_invalid_continuation() -> None:
    payload = _json(CONTRACT_DIR / "fixtures" / "scheduled-task.json")
    payload["command"]["payload"]["api_key"] = "must-not-persist"
    with pytest.raises(ValidationError, match="secret reference"):
        ScheduledTask.model_validate(payload)

    payload = _json(CONTRACT_DIR / "fixtures" / "scheduled-task.json")
    payload["continuity"] = "continue_session"
    with pytest.raises(ValidationError, match="requires target.sessionId"):
        ScheduledTask.model_validate(payload)

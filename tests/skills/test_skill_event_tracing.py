from __future__ import annotations

import json
from pathlib import Path

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from ksadk.skills.events import SkillEvent
from ksadk.skills.models import SkillRef
from ksadk.skills.observability import project_skill_events


def test_evidence_fixture_has_minimum_evaluation_dimensions() -> None:
    fixture = json.loads(
        (Path(__file__).parent / "fixtures" / "skill_execution_evidence_v1.json").read_text(
            encoding="utf-8"
        )
    )

    assert fixture["command_skill"]["selection_evidence"] is True
    assert fixture["command_skill"]["consumption_evidence"] is True
    assert fixture["instruction_skill"]["execution_health"] == "not_applicable"


def test_project_skill_events_rebuilds_child_spans_and_parent_events() -> None:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("skill-event-test")
    skill_ref = SkillRef("skill-1", "version-1", "1.0.0", "report")
    events = [
        SkillEvent.create(
            "skill.candidates.resolved",
            status="completed",
            attributes={"candidate_count": 1},
            started_at=1.0,
        ),
        SkillEvent.create(
            "skill.load.started",
            status="running",
            skill_ref=skill_ref,
            skill_invocation_id="inv-1",
            started_at=1.0,
        ),
        SkillEvent.create(
            "skill.load.completed",
            status="completed",
            skill_ref=skill_ref,
            skill_invocation_id="inv-1",
            started_at=1.2,
        ),
    ]

    with tracer.start_as_current_span("execute_skills") as parent:
        project_skill_events(events, tracer=tracer)
        parent_id = parent.get_span_context().span_id

    spans = {span.name: span for span in exporter.get_finished_spans()}
    child = spans["skill.load.completed"]
    parent = spans["execute_skills"]
    assert child.parent.span_id == parent_id
    assert child.attributes["ksadk.skill.id"] == "skill-1"
    assert child.attributes["ksadk.skill.event_type"] == "skill.load.completed"
    assert child.start_time == 1_000_000_000
    assert child.end_time == 1_200_000_000
    assert [span.name for span in spans.values()].count("skill.load.completed") == 1
    assert [event.name for event in parent.events] == ["skill.candidates.resolved"]

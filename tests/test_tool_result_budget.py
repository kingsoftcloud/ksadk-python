from __future__ import annotations

import json

from ksadk.conversations.context import project_model_messages
from ksadk.sessions import SessionEvent
from ksadk.tools.result_budget import ToolResultBudget, budget_tool_output


def test_budget_tool_output_persists_large_text(tmp_path):
    budget = ToolResultBudget(
        max_chars=20,
        preview_chars=8,
        persist_threshold_chars=12,
        persist_dir=tmp_path,
    )

    result = budget_tool_output(
        tool_name="run_command",
        field_name="stdout",
        value="abcdefghijklmnopqrstuvwxyz",
        metadata={"tool_use_id": "call_123"},
        budget=budget,
    )

    assert result["stdout"] == "abcdefgh"
    assert result["truncated"] is True
    assert result["original_chars"] == 26
    assert result["preview_chars"] == 8
    assert result["persisted"]["mime_type"] == "text/plain"
    persisted_path = tmp_path / "call_123.stdout.txt"
    assert result["persisted"]["path"] == str(persisted_path)
    assert persisted_path.read_text(encoding="utf-8") == "abcdefghijklmnopqrstuvwxyz"


def test_budget_tool_output_serializes_json_values(tmp_path):
    budget = ToolResultBudget(
        max_chars=10,
        preview_chars=5,
        persist_threshold_chars=8,
        persist_dir=tmp_path,
    )

    result = budget_tool_output(
        tool_name="web_search",
        field_name="results",
        value={"items": ["alpha", "beta"]},
        metadata={"tool_use_id": "search_1"},
        budget=budget,
    )

    assert result["results"].startswith("{")
    assert result["persisted"]["mime_type"] == "application/json"
    persisted = json.loads((tmp_path / "search_1.results.json").read_text(encoding="utf-8"))
    assert persisted == {"items": ["alpha", "beta"]}


def test_project_model_messages_projects_persisted_tool_result_preview():
    events = [
        SessionEvent(
            id="evt-1",
            session_id="sess-1",
            author="agent",
            event_type="tool_result",
            content={
                "role": "tool",
                "parts": [
                    {
                        "text": {
                            "stdout": "short preview",
                            "truncated": True,
                            "persisted": {
                                "path": "sessions/sess-1/tool-results/call.stdout.txt",
                                "mime_type": "text/plain",
                            },
                        }
                    }
                ],
            },
            timestamp="2026-07-02T00:00:00Z",
            seq_id=1,
        )
    ]

    projected = project_model_messages(events)

    assert projected == [
        {
            "role": "user",
            "content": "[tool_result] short preview\n[persisted-output] sessions/sess-1/tool-results/call.stdout.txt (text/plain)",
        }
    ]

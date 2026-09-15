import json
from types import SimpleNamespace
from typing import TypedDict

import pytest
from langgraph.graph import END, START, StateGraph

from ksadk.conversations.runtime_observability import _set_skill_eval_result_attributes
from ksadk.conversations.runtime_payloads import build_responses_payload
from ksadk.runners.langgraph_runner import LangGraphRunner
from ksadk.runtime.skill_eval_result import extract_skill_eval_result, skill_eval_response_fields

EVIDENCE = {
    "schema_version": "base_agent.skill_eval_result.v2",
    "run": {"mode": "no_skill", "trace_id": "a" * 32},
    "skill": {"loaded": [], "uses": []},
}


@pytest.mark.parametrize(
    "value",
    [
        None,
        {},
        {"skill_eval_result": "text"},
        {"skill_eval_result": {"schema_version": "unknown"}},
        {"messages": [{"content": json.dumps(EVIDENCE)}]},
    ],
)
def test_result_is_opt_in_and_never_extracted_from_messages(value):
    assert extract_skill_eval_result(value) == {}


def test_result_owns_its_json_and_rejects_non_json():
    fields = skill_eval_response_fields({"skill_eval_result": EVIDENCE})
    fields["skill_eval_result"]["skill"]["loaded"].append("changed")
    assert EVIDENCE["skill"]["loaded"] == []
    assert fields["trace_id"] == "a" * 32
    assert extract_skill_eval_result({"skill_eval_result": {**EVIDENCE, "invalid": object()}}) == {}
    assert "trace_id" not in skill_eval_response_fields(
        {"skill_eval_result": {**EVIDENCE, "run": {"trace_id": "0" * 32}}}
    )


def test_plain_agents_omit_result_and_evidence_never_enters_response_text():
    args = dict(output_text="hello", model="test", session_id="session")
    assert "skill_eval_result" not in build_responses_payload(**args)
    payload = build_responses_payload(**args, skill_eval_result=EVIDENCE)
    assert payload["skill_eval_result"] == EVIDENCE
    assert payload["output_text"] == "hello"
    assert "skill_eval_result" not in json.dumps(payload["output"])


def test_trace_metadata_preserves_pure_output():
    attributes = {"output.value": "hello", "langfuse.trace.output": "hello"}
    span = SimpleNamespace(set_attribute=lambda key, value: attributes.__setitem__(key, value))
    _set_skill_eval_result_attributes(span, EVIDENCE)
    assert json.loads(attributes["metadata"])["skill_eval_result"] == EVIDENCE
    assert attributes["output.value"] == attributes["langfuse.trace.output"] == "hello"


@pytest.mark.asyncio
@pytest.mark.parametrize("force_invoke", [False, True])
async def test_graph_result_reaches_runner_without_text_suffix(force_invoke):
    class State(TypedDict):
        answer: str
        skill_eval_result: dict

    graph = StateGraph(State)
    graph.add_node(
        "finalize_answer", lambda state: {"answer": "hello", "skill_eval_result": EVIDENCE}
    )
    graph.add_edge(START, "finalize_answer")
    graph.add_edge("finalize_answer", END)
    compiled = graph.compile()
    # These node outputs are what LangGraph instrumentation sees, distinct from LLM messages.
    events = [event async for event in compiled.astream_events({}, version="v2")]
    node = next(
        event
        for event in events
        if event["event"] == "on_chain_end" and event["name"] == "finalize_answer"
    )
    assert node["data"]["output"] == {"answer": "hello", "skill_eval_result": EVIDENCE}
    runner = LangGraphRunner(
        SimpleNamespace(
            entry_point="agent.py", agent_variable="agent", type=SimpleNamespace(value="langgraph")
        ),
        ".",
    )
    runner._agent = compiled
    result = await runner.invoke({"input": "hello", "_ksadk_force_graph_invoke": force_invoke})
    assert result["output"] == "hello"
    assert result["skill_eval_result"] == EVIDENCE


@pytest.mark.asyncio
async def test_canonical_stream_keeps_evidence_in_completion_metadata():
    from ksadk.events.canonical import RunCompleted

    class State(TypedDict):
        answer: str
        skill_eval_result: dict

    graph = StateGraph(State)
    graph.add_node(
        "finalize_answer", lambda state: {"answer": "hello", "skill_eval_result": EVIDENCE}
    )
    graph.add_edge(START, "finalize_answer")
    graph.add_edge("finalize_answer", END)
    runner = LangGraphRunner(
        SimpleNamespace(
            entry_point="agent.py", agent_variable="agent", type=SimpleNamespace(value="langgraph")
        ),
        ".",
    )
    runner._agent = graph.compile()
    events = [event async for event in runner.stream_canonical_events({"input": "hello", "run_id": "eval-run"})]
    completed = next(event for event in events if isinstance(event, RunCompleted))
    assert completed.source.metadata["metrics"]["skill_eval_result"] == EVIDENCE

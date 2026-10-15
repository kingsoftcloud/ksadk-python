"""Source capacity must fit the real planner after sealing an entire read batch."""

import copy
import hashlib
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from ksadk.harness.artifact_adapters.native_page_budget import ArtifactPagePolicy
from ksadk.harness.artifact_adapters.native_tool import (
    install_artifact_tool,
)
from ksadk.harness.native.artifact_read_capacity import artifact_group_capacity
from ksadk.harness.native.context_driver import ContextDriver
from ksadk.harness.native.context_runtime import CompactionHistory, CompactionPolicy, ContextPlanner
from ksadk.harness.native.context_runtime_contracts import _resume, _summary
from ksadk.harness.native.effective_configuration import effective_run_configuration
from ksadk.harness.native.engine import NativeKernel
from ksadk.harness.native.machine import next_transition, reduce_tool
from ksadk.harness.native.model import ModelProfile, ModelToolCall
from ksadk.harness.native.ports import ArtifactRef, RunLimits, ToolResult, digest
from ksadk.harness.native.state import RunPhase
from ksadk.harness.native.tool_budget import (
    model_instructions,
    model_tool_surface,
    model_turn_control,
)
from ksadk.harness.native.transitions import commit_transition
from ksadk.harness.stores.native_memory import MemoryRunStore
from ksadk.studio.native_product import _estimated_token_counter, _studio_context_planner
from ksadk.studio.native_read_budget import StudioSourceReadPolicy
from tests.harness.native.test_kernel import SCOPE, ScriptedModel, config


class Ports:
    async def execute(self, _request):
        pytest.fail("A capacity quote cannot execute a tool.")

    async def read(self, _request):
        pytest.fail("A capacity quote cannot read an artifact.")


def setup(*, revision=98, old_refs=8, progress=9, summary_max=1024, message_extra=0,
          instructions="Report only observed sources.", max_tool_calls=1000):
    profile = ModelProfile(profile_id="capacity", profile_version="1", provider_family="test",
                           context_window=8000, max_output_tokens=1024)
    planner = _studio_context_planner(profile)
    planner.policy = planner.policy.model_copy(update={"summary_max_tokens": summary_max})
    if message_extra:
        ordinary = planner.count_message_tokens
        planner.count_message_tokens = lambda message: ordinary(message) + message_extra
        planner.tokenizer_id += f"-frame-{message_extra}"
    policy = StudioSourceReadPolicy(
        profile, _estimated_token_counter, "test-units/v1", planner.count_message_tokens,
        retain_requested_source=True, artifact_only_fallback=True,
        archive_model_policy="archived-model-view/v5", artifact_pages=True,
        artifact_result_max_bytes=65536, context_planner=planner,
    )
    state = None
    store = SimpleNamespace(load=lambda scope, run_id: state)
    source = policy.artifact_policy(
        store, workspace_revision="unavailable", workspace_tracking=False,
    )
    base = config().model_copy(update={"profile": profile, "tools": (),
                                     "limits": RunLimits(max_tool_calls=max_tool_calls),
                                     "instructions": instructions})
    configuration, port = install_artifact_tool(base, Ports(), Ports(), enable_search=True,
                                               page_policy=source)
    kernel = NativeKernel(store=MemoryRunStore(), scope=SCOPE, configuration=configuration,
                          model=ScriptedModel(), context_planner=planner)
    initial = kernel.start(goal="Inspect the requested sources.", session_id="capacity", run_id="r")
    messages = list(initial.messages)
    for i in range(old_refs):
        messages.extend((
            {"role": "assistant", "content": "", "tool_calls": [{
                "id": f"old-{i}", "type": "function", "function": {
                    "name": "ArtifactRead", "arguments": json.dumps({"uri": f"artifact://old/{i}"}),
                }}]},
            {"role": "tool", "tool_call_id": f"old-{i}",
             "content": '{"status":"ok","content":"old"}',
             "receipt_ref": f"r:effect:{i+1}", "receipt_goal_revision": initial.goal_revision,
             "receipt_tool_call": i+1},
        ))
    messages.extend({"role": "assistant", "content": "Earlier observations. " * 60}
                    for _ in range(30))
    calls = tuple(ModelToolCall(call_id=f"page-{i}", name="ArtifactRead",
                               arguments={"uri": f"artifact://source/{i}", "limit_chars": 32000})
                  for i in range(8))
    messages.append({"role": "assistant", "content": "", "tool_calls": [{
        "id": c.call_id, "type": "function", "function": {
            "name": c.name, "arguments": json.dumps(c.arguments),
        }} for c in calls]})
    text = "Observation 汉字.\n" * 2000
    refs = tuple(ArtifactRef(uri=c.arguments["uri"], size_bytes=len(text.encode()),
                            content_hash="sha256:" + hashlib.sha256(text.encode()).hexdigest(),
                            mime_type="text/plain") for c in calls)
    state = initial.model_copy(update={
        "phase": RunPhase.EXECUTING_TOOLS, "revision": revision, "model_calls": 2,
        "tool_calls": old_refs, "messages": tuple(messages),
        "pending_tools": tuple(c.model_dump(mode="json") for c in calls),
        "observed_progress_revision": progress,
        "artifact_refs": tuple(ref.model_dump(mode="json") for ref in refs),
    })
    state = next_transition(state, configuration).snapshot

    def set_state(value):
        nonlocal state
        state = value

    return configuration, planner, policy, source, port, kernel, state, refs, text, set_state


@pytest.mark.parametrize("revision,old_refs,progress,summary_max,message_extra", [
    (8, 8, 9, 1024, 0), (98, 8, 99, 1024, 0), (998, 9, 999, 512, 31),
    (9998, 7, 9, 2048, 17),
])
def test_sealed_full_batch_retained_with_real_planner(
    revision, old_refs, progress, summary_max, message_extra,
):
    cfg, planner, policy, source, port, kernel, state, refs, text, save = setup(
        revision=revision, old_refs=old_refs, progress=progress,
        summary_max=summary_max, message_extra=message_extra,
    )
    original = copy.deepcopy(state)
    before_store = kernel.store.load(SCOPE, "r").model_dump_json()
    first = artifact_group_capacity(state, cfg, kernel.context)
    assert state == original and kernel.store.load(SCOPE, "r").model_dump_json() == before_store
    assert first.summary_tokens == (
        min(summary_max, cfg.profile.max_output_tokens)
        + planner._count_message(_summary("")) + planner._count_message(_resume())
    )
    quoted = []
    while state.pending_tools:
        # This is execution by the real pure transition reducers, with a new
        # page selected for every call. The quote itself cannot commit anything.
        prepared = next_transition(state.model_copy(update={
            "pending_intent": None, "revision": state.revision - 1,
        }), cfg)
        calls = prepared.intents[0].payload.get("calls") or [prepared.intents[0].payload["call"]]
        updated = state
        for raw in calls:
            call = ModelToolCall.model_validate(raw)
            budget = port._page_policy.resolve(SimpleNamespace(
                scope=SCOPE, run_id="r", call=call), cfg.tools)
            assert budget.source_group_size == 8
            ref = next(r for r in refs if r.uri == call.arguments["uri"])
            page = budget.select({"uri": ref.uri, "content": text, "offset_chars": 0,
                                  "next_offset_chars": len(text), "complete": True,
                                  "content_hash": ref.content_hash, "size_bytes": ref.size_bytes,
                                  "mime_type": ref.mime_type})
            assert page["content"] and budget.fits(page)
            updated = reduce_tool(
                updated.model_copy(update={"pending_intent": state.pending_intent}),
                                  ToolResult(status="ok", content=page),
                                  definition=next(t for t in cfg.tools if t.name == "ArtifactRead"))
            quoted.append(page)
        state = commit_transition(state, updated, "effect.received").snapshot
        if state.pending_tools:
            state = next_transition(state, cfg).snapshot
            save(state)
    state = next_transition(state, cfg).snapshot
    plan = planner.plan(
        source_revision=state.revision, model_turn=state.model_calls, profile=cfg.profile,
        instructions=model_instructions(state, cfg), tools=model_tool_surface(state, cfg),
        context_tools=cfg.tools, messages=state.messages,
        continuity=kernel.context.continuity(state, cfg), contract=cfg.contract,
        history=CompactionHistory(), turn_control=model_turn_control(state, cfg),
    )
    assert plan.action == "compact", plan.reason
    retained = plan.request.retained_messages
    observed = [json.loads(m["content"])["content"] for m in retained
                if m.get("tool_call_id", "").startswith("page-")]
    assert observed == quoted and len(observed) == 8
    assert (sum(planner.count_model_message(m, cfg.profile) for m in retained)
            + first.summary_tokens <= plan.request.budget.input_capacity)


@pytest.mark.parametrize("changed", ["configuration", "context", "command", "mixed", "duplicate"])
def test_unknown_or_intervening_state_has_no_strong_quote(changed):
    cfg, planner, policy, source, port, kernel, state, *_ = setup()
    if changed == "configuration":
        state = state.model_copy(update={"configuration_digest": "foreign"})
    elif changed == "context":
        state = state.model_copy(update={"context_binding": "foreign"})
    elif changed == "command":
        state = state.model_copy(update={"revision": state.revision+1})
    else:
        messages = copy.deepcopy(state.messages)
        calls = messages[-1]["tool_calls"]
        if changed == "mixed":
            calls[0]["function"]["name"] = "unknown"
        else:
            calls[1]["function"]["arguments"] = calls[0]["function"]["arguments"]
        state = state.model_copy(update={"messages": messages})
    if changed in {"configuration", "context"}:
        with pytest.raises(ValueError, match="configuration_mismatch"):
            artifact_group_capacity(state, cfg, kernel.context)
    else:
        assert artifact_group_capacity(state, cfg, kernel.context) is None


def test_summary_policy_and_message_framing_change_capacity_identity():
    cfg, planner, policy, *_ = setup()
    smaller = ContextPlanner(count_tokens=len, tokenizer_id="characters/v1",
                             policy=CompactionPolicy(summary_max_tokens=64))
    assert smaller.summary_reservation(cfg.profile) == 64 + len(json.dumps(
        _summary(""), ensure_ascii=False, sort_keys=True, separators=(",", ":"))) + len(json.dumps(
            _resume(), ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    assert replace(policy, context_planner=smaller).digest != policy.digest


def test_configuration_binding_waits_for_all_reserved_tools_and_rejects_wrong_digest():
    cfg, planner, policy, source, port, kernel, state, *_ = setup()
    assert {t.name for t in cfg.tools} == {"ArtifactRead", "ArtifactSearch"}
    with pytest.raises(ValueError, match="unbound"):
        source.resolve(None, ())
    # A reconstructed installation binds to exactly the same immutable config.
    cfg2, port2 = install_artifact_tool(
        cfg, Ports(), Ports(), enable_search=True, page_policy=source,
    )
    assert cfg2 == cfg and port2._page_policy.digest == port._page_policy.digest
    request = SimpleNamespace(scope=SCOPE, run_id="r",
                              call=ModelToolCall.model_validate(state.pending_tools[0]))
    budget1 = port._page_policy.resolve(request, cfg.tools)
    budget2 = port2._page_policy.resolve(request, cfg2.tools)
    assert (budget1.max_units, budget1.max_result_units) == (
        budget2.max_units, budget2.max_result_units)
    assert budget1.count_result_message({"content": "known"}) == (
        budget2.count_result_message({"content": "known"}))
    foreign = cfg.model_copy(update={"instructions": "Different child instructions and contract."})
    foreign_port = source.bind_configuration(foreign)
    with pytest.raises(ValueError, match="configuration_mismatch"):
        foreign_port.resolve(request, foreign.tools)
    bad = ArtifactPagePolicy(source.digest, source.resolve, bind_configuration=lambda c:
                             ArtifactPagePolicy("sha256:"+"0"*64, source.resolve))
    with pytest.raises(ValueError, match="configuration_binding_invalid"):
        install_artifact_tool(cfg, Ports(), Ports(), enable_search=True, page_policy=bad)


def test_effective_configuration_matches_kernel_contract_and_output_schema():
    cfg, planner, policy, source, port, kernel, state, *_ = setup()
    assert effective_run_configuration(cfg, state) == kernel.effective_configuration(state)
    contract = cfg.contract.model_copy(update={
        "revision": "child", "allowed_tool_names": ("ArtifactRead",),
        "final_submission_schema": {"type": "object", "properties": {"answer": {"type": "string"}},
                                    "required": ["answer"], "additionalProperties": False},
        "final_submission_payload_key": "answer",
    })
    state = state.model_copy(update={"task_contract": contract.model_dump(mode="json"),
                                     "contract_digest": digest(contract)})
    effective = effective_run_configuration(cfg, state)
    assert effective == kernel.effective_configuration(state)
    assert effective.contract == contract and cfg.contract != contract
    assert effective.final_submission_schema == contract.final_submission_schema
    assert effective.final_submission_payload_key == "answer"
    with pytest.raises(ValueError, match="contract identity"):
        effective_run_configuration(cfg, state.model_copy(update={"contract_digest": "wrong"}))


def test_parent_and_child_bind_separate_actual_configurations():
    parent = setup(instructions="Parent orchestration. " * 20, max_tool_calls=1000)
    child = setup(instructions="Child source reporting.", max_tool_calls=16)
    quotes = []
    for current, other in ((parent, child), (child, parent)):
        cfg, planner, policy, source, port, kernel, state, *_ = current
        quote = artifact_group_capacity(state, cfg, kernel.context)
        assert quote is not None
        quotes.append(quote)
        request = SimpleNamespace(scope=SCOPE, run_id="r",
                                  call=ModelToolCall.model_validate(state.pending_tools[0]))
        page_budget = port._page_policy.resolve(request, cfg.tools)
        assert page_budget.max_result_units == quote.result_message_units
        # A legitimate configuration for another Run still cannot price this
        # Run's contract, finalization control, or complete model tool surface.
        with pytest.raises(ValueError, match="configuration_mismatch"):
            source.bind_configuration(other[0]).resolve(request, other[0].tools)
    assert quotes[0].fixed_tokens != quotes[1].fixed_tokens


def test_mandatory_system_messages_remain_in_full_request_capacity():
    cfg, planner, policy, source, port, kernel, state, *_ = setup()
    before = artifact_group_capacity(state, cfg, kernel.context)
    state = state.model_copy(update={"messages": (
        {"role": "system", "content": "Pinned system requirement. " * 100}, *state.messages,
    )})
    after = artifact_group_capacity(state, cfg, kernel.context)
    assert after.fixed_tokens == before.fixed_tokens
    assert after.result_message_units < before.result_message_units
    assert after.source_units < before.source_units


@pytest.mark.parametrize("working_set_count", [0, 7])
def test_insufficient_read_working_set_cannot_claim_retained_group_capacity(working_set_count):
    cfg, planner, policy, source, port, kernel, state, *_ = setup()
    planner.policy = planner.policy.model_copy(update={"read_working_set_count": working_set_count})
    driver = ContextDriver(planner, "unavailable")
    state = state.model_copy(update={"context_binding": driver.binding})
    assert artifact_group_capacity(state, cfg, driver) is None

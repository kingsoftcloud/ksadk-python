"""Host-tokenized context planning and sealed, low-trust compaction."""

import base64
import copy
import hashlib
import json
import re

import pytest

from ksadk.harness.native.completion import AcceptanceItem, TaskContract
from ksadk.harness.native.context import ExecutionContinuityState, _tool_groups
from ksadk.harness.native.context_runtime import (
    CompactionController,
    CompactionHistory,
    CompactionPolicy,
    CompactionRequest,
    CompactionResult,
    ContextPlanner,
    ContextRuntimeError,
    source_context_digest,
)
from ksadk.harness.native.model import ModelProfile
from ksadk.harness.native.ports import ToolDefinition, digest

READ_TOOLS = (
    ToolDefinition(
        name="read_workspace_file",
        description="Source reader",
        parameters={},
        read_only=True,
        context_role="workspace_read",
    ),
)

IMAGE_URL = "data:image/png;base64," + base64.b64encode(b"fixture-image" * 1000).decode()


def media_messages():
    return (
        {"role": "user", "content": "Build the game."},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "browser-shot",
                    "type": "function",
                    "function": {
                        "name": "native_browser",
                        "arguments": '{"action":"screenshot"}',
                    },
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "browser-shot",
            "content": [
                {"type": "text", "text": "artifact://opaque/game " + "historical " * 2400},
                {"type": "image_url", "image_url": {"url": IMAGE_URL}},
            ],
        },
        {"role": "assistant", "content": "Save/load still needs verification."},
    )


def typed_count(message):
    """Synthetic fixture image charge; not claimed to match any real provider."""
    image_tokens = 0
    raw = copy.deepcopy(message)
    if isinstance(raw.get("content"), list):
        for index, block in enumerate(raw["content"]):
            if block["type"] == "image_url":
                image_tokens += 100
                raw["content"][index] = {"type": "text", "text": "[image]"}
    return count_tokens(json.dumps(raw)) + image_tokens


def count_tokens(text):
    """Synthetic test tokenizer units; never advertised as real provider tokens."""
    return len(re.findall(r"\w+|[^\w\s]", text))


def contract():
    return TaskContract(
        revision="2",
        items=(
            AcceptanceItem(
                acceptance_id="save",
                description="Save state must survive refresh.",
                verifier_ref="native.file",
                verifier_version="1",
                verification_method="deterministic",
            ),
        ),
    )


def continuity(**overrides):
    return ExecutionContinuityState(
        **{
            "goal": "Build the game.",
            "goal_revision": 2,
            "contract_digest": digest(contract()),
            "required_acceptance_ids": ("save",),
            "pending_work": ("Verify save/load after refresh.",),
            "failure_fingerprints": ("save-reset",),
            "evidence_refs": ("evidence://movement",),
            "artifact_refs": ("artifact://game",),
            "next_action": "Test save/load.",
            "workspace_revision": "workspace-v3",
            **overrides,
        }
    )


def profile(window=4000):
    return ModelProfile(
        profile_id="fixture",
        profile_version="1",
        provider_family="test",
        context_window=window,
        max_output_tokens=256,
    )


def planner(**policy):
    return ContextPlanner(
        count_tokens=count_tokens,
        tokenizer_id="synthetic-test-v1",
        policy=CompactionPolicy(summary_max_tokens=128, **policy),
    )


def messages(size=2400):
    return (
        {"role": "user", "content": "Build the game."},
        {"role": "assistant", "content": " ".join(["historical"] * size)},
        {"role": "assistant", "content": "I will verify save/load."},
    )


def plan(instance=None, **overrides):
    return (instance or planner()).plan(
        **{
            "source_revision": 7,
            "model_turn": 4,
            "profile": profile(),
            "instructions": "Follow the user goal.",
            "tools": READ_TOOLS,
            "messages": messages(),
            "continuity": continuity(),
            "contract": contract(),
            **overrides,
        }
    )


def seal(instance, request, summary="Movement works; save/load verification remains.", **overrides):
    return CompactionController(instance).seal(
        request,
        summary,
        **{
            "current_revision": 9,
            "current_continuity": continuity(),
            "current_messages": messages(),
            **overrides,
        },
    )


def test_ready_context_preserves_goal_and_required_descriptions_without_compaction():
    raw = messages(2)
    result = plan(messages=raw)
    assert result.action == "ready"
    assert result.prepared.messages == raw
    instructions = result.prepared.instructions
    assert continuity().goal in instructions
    assert contract().items[0].description in instructions
    for value in (
        *continuity().pending_work,
        *continuity().failure_fingerprints,
        *continuity().evidence_refs,
        *continuity().artifact_refs,
        continuity().next_action,
    ):
        assert value in instructions
    assert result.prepared.tokenizer_id == "synthetic-test-v1"


@pytest.mark.parametrize("failed", [False, True])
def test_new_reader_observations_are_seen_before_compaction_without_rewriting_receipts(failed):
    calls, results = [], []
    for i in range(4):
        path = f"source-{i}.txt"
        calls.append(
            {
                "id": f"fresh-{i}",
                "type": "function",
                "function": {"name": "source_reader", "arguments": json.dumps({"path": path})},
            }
        )
        results.append(
            {
                "role": "tool",
                "tool_call_id": f"fresh-{i}",
                "content": json.dumps(
                    {
                        "status": "error" if failed and i == 2 else "ok",
                        "content": {
                            "ok": not (failed and i == 2),
                            "path": path,
                            "content": f"FIRST-{i} " + "source fact " * 1800 + f" LAST-{i}",
                        },
                    }
                ),
            }
        )
    raw = (
        {"role": "user", "content": continuity().goal},
        {"role": "assistant", "content": None, "tool_calls": calls},
        *results,
    )
    saved = copy.deepcopy(raw)
    instance = planner(fresh_read_max_chars=1000)
    tools = (
        ToolDefinition(
            name="source_reader",
            description="Reader alias",
            parameters={},
            read_only=True,
            context_role="workspace_read",
        ),
    )
    result = plan(instance, messages=raw, tools=tools)
    if failed:
        assert result.action != "ready"
    else:
        assert result.action == "ready", result.reason
        assert result.reason == "fresh_reader_observations_within_budget"
        for i, message in enumerate(result.prepared.messages[-4:]):
            receipt = json.loads(message["content"])
            excerpt = receipt["content"]["content"]
            assert excerpt["type"] == "workspace_read_excerpt"
            assert f"FIRST-{i}" in excerpt["head"] and f"LAST-{i}" in excerpt["tail"]
            assert receipt["content"]["path"] == f"source-{i}.txt"
            assert message["tool_call_id"] == f"fresh-{i}"
        assert result.prepared.counted_input_tokens + profile().max_output_tokens <= 4000
    assert raw == saved


def test_fixed_request_overhead_does_not_consume_the_variable_compaction_threshold():
    instance = planner()
    result = plan(
        instance,
        profile=profile(4000),
        instructions="policy " * 1200,
        messages=(
            {"role": "user", "content": "Build the game."},
            {"role": "assistant", "content": "history " * 1200},
        ),
    )
    assert result.action == "ready"
    assert result.prepared is not None
    # The old whole-window threshold would compact despite ample variable
    # capacity because it charged the pinned tool/instruction surface twice.
    assert result.prepared.counted_input_tokens + 256 > 4000 * instance.policy.trigger_ratio


def test_multimodal_without_host_message_counter_blocks_before_text_tokenization():
    counted = []

    def text_counter(text):
        counted.append(text)
        return count_tokens(text)

    instance = ContextPlanner(count_tokens=text_counter, tokenizer_id="text-only")
    result = plan(instance, messages=media_messages())
    assert result.action == "blocked"
    assert result.reason == "context_multimodal_counter_unavailable"
    assert all("data:image" not in text for text in counted)


def test_multimodal_summary_preserves_image_blocks_and_never_serializes_pixels_as_text():
    text_calls, message_calls = [], []

    def text_counter(text):
        text_calls.append(text)
        return count_tokens(text)

    def message_counter(message):
        message_calls.append(copy.deepcopy(message))
        return typed_count(message)

    instance = ContextPlanner(
        count_tokens=text_counter,
        count_message_tokens=message_counter,
        tokenizer_id="synthetic-vision-v1",
        policy=CompactionPolicy(summary_max_tokens=128),
    )
    raw = media_messages()
    before = copy.deepcopy(raw)
    result = plan(instance, messages=raw, profile=profile(window=3400))
    assert result.action == "compact"
    summary = result.request.summary_messages
    blocks = [block for message in summary for block in message["content"]]
    images = [block for block in blocks if block["type"] == "image_url"]
    texts = [block["text"] for block in blocks if block["type"] == "text"]
    assert len(images) == 1 and images[0]["image_url"]["url"] == IMAGE_URL
    assert all("data:image" not in text for text in texts + text_calls)
    assert any("artifact://opaque/game" in text for text in texts)
    history = json.loads(texts[0])
    known_refs = {message.message_id for message in result.request.old_messages}
    assert all(item["source_message_ref"] in known_refs for item in history["transcript"])
    assert all(message["role"] == "user" for message in summary)
    assert not any("tool_calls" in message for message in summary)
    assert any(message in message_calls for message in summary)
    assert (
        result.request.summary_counted_input_tokens + profile().max_output_tokens
        <= profile().context_window
    )
    sealed = seal(instance, result.request, current_messages=raw)
    assert sealed.history.tokenizer_id == "synthetic-vision-v1"
    assert (
        sealed.prepared.counted_input_tokens + profile().max_output_tokens
        <= profile().context_window
    )
    assert raw == before


@pytest.mark.parametrize(
    "counter", [lambda _message: True, lambda _message: -1, lambda _message: 0]
)
def test_invalid_host_message_counter_returns_stable_blocked_reason(counter):
    instance = ContextPlanner(
        count_tokens=count_tokens, count_message_tokens=counter, tokenizer_id="bad"
    )
    result = plan(instance, messages=media_messages())
    assert result.action == "blocked"
    assert result.reason == "context_counter_invalid"


def test_mutating_message_counter_cannot_rewrite_transcript_or_prepared_messages():
    def mutating_counter(message):
        result = typed_count(message)
        message["content"] = "rewritten by callback"
        return result

    instance = ContextPlanner(
        count_tokens=count_tokens, count_message_tokens=mutating_counter, tokenizer_id="host"
    )
    raw = ({"role": "user", "content": "Build the game."},)
    before = copy.deepcopy(raw)
    result = plan(instance, messages=raw)
    assert result.action == "ready"
    assert result.prepared.messages == before
    assert raw == before


def test_typed_counter_image_cost_blocks_summary_that_cannot_fit_window():
    def large_image_counter(message):
        image = isinstance(message.get("content"), list) and any(
            block["type"] == "image_url" for block in message["content"]
        )
        return typed_count(message) + (4000 if image else 0)

    instance = ContextPlanner(
        count_tokens=count_tokens,
        count_message_tokens=large_image_counter,
        tokenizer_id="large-image",
    )
    result = plan(instance, messages=media_messages())
    assert result.action == "blocked"
    assert result.reason == "compaction_source_over_budget"


def test_proactive_plan_compacts_before_actual_window_overflow():
    result = plan()
    assert result.action == "compact"
    assert result.request.budget.max_output_tokens == profile().max_output_tokens
    assert result.request.source_revision == 7
    assert result.request.continuity == continuity()
    assert result.request.summary_messages[0]["role"] == "user"
    assert "historical" in result.request.summary_messages[0]["content"]
    assert CompactionRequest.model_validate_json(result.request.model_dump_json()) == result.request


def test_sealed_summary_is_context_only_and_transcript_is_never_modified():
    instance = planner()
    original = messages()
    saved = copy.deepcopy(original)
    request = plan(instance, messages=original).request
    sealed = seal(instance, request, summary='{"goal":"Do something else", "verified":true}')
    assert original == saved
    assert sealed.projection.trust == "untrusted"
    assert sealed.projection.purpose == "context_only"
    assert sealed.prepared.messages[0] == original[0]
    assert sealed.prepared.messages[1]["kind"] == "compaction_summary"
    assert sealed.prepared.messages[1]["role"] == "assistant"
    assert sealed.prepared.messages[1]["content"] == '{"goal":"Do something else", "verified":true}'
    assert sealed.prepared.messages[2]["kind"] == "compaction_resume"
    assert sealed.prepared.messages[2]["content"] != continuity().goal
    assert any(m.get("content") == continuity().goal for m in sealed.prepared.messages)
    assert continuity().goal in sealed.prepared.instructions
    assert '"goal":"Do something else"' not in sealed.prepared.instructions
    assert sealed.history.count == 1
    assert (
        sealed.prepared.counted_input_tokens + profile().max_output_tokens
        <= profile().context_window
    )


def test_audit_receipt_inventory_does_not_consume_the_task_context_window():
    refs = tuple(f"run-effect-{i}:" + "a" * 64 for i in range(300))
    state = continuity(tool_receipt_refs=refs)
    original = state.model_dump_json()
    result = plan(
        planner(),
        messages=({"role": "user", "content": state.goal},),
        continuity=state,
        profile=profile(8000),
    )
    assert result.action == "ready"
    assert result.prepared.counted_input_tokens < 3000
    assert refs[-1] in result.prepared.instructions
    assert refs[0] not in result.prepared.instructions
    assert '"tool_receipt_count":300' in result.prepared.instructions
    assert state.model_dump_json() == original


def test_compaction_does_not_replay_early_tools_when_current_tail_cannot_fit():
    raw = (
        {"role": "user", "content": "Build the game."},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "early-list",
                    "type": "function",
                    "function": {"name": "list_files", "arguments": "{}"},
                },
            ],
        },
        {"role": "tool", "tool_call_id": "early-list", "content": "app.js"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "latest-read",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": "{}"},
                },
            ],
        },
        {"role": "tool", "tool_call_id": "latest-read", "content": "recent-source " * 6000},
    )
    # This receipt is already historical: a later task response exists.
    # Fresh ordinary groups are covered by test_fresh_tool_observation.
    raw = (*raw, {"role": "assistant", "content": "Observations received; continue."})
    instance = planner()
    result = plan(instance, messages=raw)
    assert result.action == "compact"
    assert result.request.retained_messages == (raw[0], raw[-1])
    sealed = seal(
        instance,
        result.request,
        summary="The file was read; implementation details are known.",
        current_messages=raw,
    )
    assert not any(m.get("tool_calls") for m in sealed.prepared.messages)
    assert sealed.prepared.messages[0]["role"] == "user"
    assert sealed.prepared.messages[1]["role"] == "assistant"


def test_append_after_compaction_reuses_projection_without_reintroducing_old_history():
    instance = planner()
    first = plan(instance)
    sealed = seal(instance, first.request)
    appended = (*messages(), {"role": "assistant", "content": "new observation"})
    second = plan(
        instance, messages=appended, source_revision=20, model_turn=6, history=sealed.history
    )
    assert second.action == "ready"
    assert any(m.get("content") == "new observation" for m in second.prepared.messages)
    assert not any("historical historical" in str(m) for m in second.prepared.messages)
    assert any(m.get("kind") == "compaction_summary" for m in second.prepared.messages)


def test_complete_parallel_tool_batch_is_preserved_or_removed_as_a_unit():
    raw = (
        messages()[0],
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {"id": "a", "type": "function", "function": {"name": "read", "arguments": "{}"}},
                {"id": "b", "type": "function", "function": {"name": "read", "arguments": "{}"}},
            ],
        },
        {"role": "tool", "tool_call_id": "a", "content": " ".join(["history"] * 1200)},
        {"role": "tool", "tool_call_id": "b", "content": " ".join(["history"] * 1000)},
        {"role": "assistant", "content": "next"},
    )
    instance = planner()
    result = plan(instance, messages=raw, force_compaction=True)
    assert result.action == "compact"
    retained = set(result.request.retained_message_refs)
    tool_refs = {message.message_id for message in result.request.old_messages[1:4]}
    assert not retained.intersection(tool_refs) or tool_refs.issubset(retained)
    sealed = seal(instance, result.request, current_messages=raw)
    assert sealed.projection.messages


def test_pending_tool_protocol_survives_compaction_with_exact_arguments():
    raw = (
        *messages(),
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "pending",
                    "type": "function",
                    "function": {"name": "read", "arguments": '{"path":"save.json"}'},
                },
            ],
        },
    )
    current = continuity(pending_tool_call_ids=("pending",))
    instance = planner()
    result = plan(instance, messages=raw, continuity=current)
    assert result.action == "compact"
    sealed = seal(instance, result.request, current_messages=raw, current_continuity=current)
    assert sealed.prepared.messages[-1] == raw[-1]


@pytest.mark.parametrize(
    "change",
    [
        {"goal_revision": 3},
        {"workspace_revision": "changed"},
        {"pending_work": ()},
        {"evidence_refs": ()},
        {"next_action": None},
    ],
)
def test_changed_host_continuity_cannot_seal_stale_summary(change):
    instance = planner()
    with pytest.raises(ContextRuntimeError, match="context_continuity_changed"):
        seal(instance, plan(instance).request, current_continuity=continuity(**change))


def test_semantic_source_must_match_even_when_event_revision_can_advance():
    instance = planner()
    request = plan(instance).request
    assert seal(instance, request, current_revision=99).projection.source_revision == 7
    with pytest.raises(ContextRuntimeError, match="context_source_changed"):
        seal(
            instance,
            request,
            current_messages=(*messages(), {"role": "user", "content": "new request"}),
        )


@pytest.mark.parametrize(
    "summary,reason",
    [
        (" ", "context_summary_empty"),
        ("word " * 129, "context_summary_budget_exceeded"),
    ],
)
def test_invalid_summary_cannot_be_committed(summary, reason):
    instance = planner()
    with pytest.raises(ContextRuntimeError, match=reason):
        seal(instance, plan(instance).request, summary=summary)


def test_small_error_batch_can_fit_without_double_encoding_or_duplicate_host_metadata():
    from ksadk.harness.native.context_runtime import _summary_continuation

    instance = ContextPlanner(
        count_tokens=count_tokens,
        tokenizer_id="synthetic-failed-batch-v1",
        policy=CompactionPolicy(summary_max_tokens=128),
    )
    raw = (
        {
            "role": "assistant",
            "content": "Recheck these observations.",
            "tool_calls": [
                {
                    "id": f"failed-{i}",
                    "function": {"name": "read_chunk", "arguments": json.dumps({"index": i})},
                }
                for i in range(7)
            ],
        },
        *(
            {
                "role": "tool",
                "tool_call_id": f"failed-{i}",
                "content": json.dumps({"status": "error", "content": {"code": "out_of_order"}}),
                "receipt_tool_call": i + 1,
                "receipt_goal_revision": 0,
                "receipt_ref": f"run:effect:{i + 1}",
            }
            for i in range(7)
        ),
    )
    before = copy.deepcopy(raw)
    entries = tuple(
        {"ref": f"source-{i}", "message": {**m, "source_message_ref": f"source-{i}"},
         "images": [], "unit": "one-batch", "payload_digest": f"digest-{i}"}
        for i, m in enumerate(raw)
    )
    previous, instructions = "The prior observations are retained.", "Summarize historical data."
    compact = copy.deepcopy(list(entries))
    compact[0]["message"]["tool_calls"] = [
        {**c, "function": {**c["function"], "arguments": json.loads(c["function"]["arguments"])}}
        for c in compact[0]["message"]["tool_calls"]
    ]
    for entry in compact[1:]:
        m = entry["message"]
        m["content"] = json.loads(m["content"])
        m.pop("receipt_tool_call")
        m.pop("receipt_goal_revision")
    _, compact_count = instance._summary_projection(compact, previous, instructions)
    _, original_count = instance._summary_projection(entries, previous, instructions)
    reserve = 128 + (
        instance._count_message({"role": "assistant", "content": ""})
        + instance._count_message(_summary_continuation())
    ) * 3
    cfg = profile().model_copy(update={"context_window": compact_count + 128 + reserve,
                                       "max_output_tokens": 128, "max_continuations": 3})
    assert not instance._summary_request_fits(original_count, cfg)
    projected, counted, remaining = instance._next_chunk(
        entries, previous, instructions, cfg, {e["ref"]: m for e, m in zip(entries, raw)}
    )
    assert not remaining
    assert instance._summary_request_fits(counted, cfg)
    transcript = json.loads(projected[0]["content"])["transcript"]
    assert len(transcript[0]["tool_calls"]) == 7
    for i, message in enumerate(transcript[1:]):
        assert message["tool_call_id"] == f"failed-{i}"
        assert message["content"] == json.loads(raw[i + 1]["content"])
        assert message["source_message_ref"] == f"source-{i + 1}"
        assert message["receipt_ref"] == f"run:effect:{i + 1}"
    assert raw == before


@pytest.mark.parametrize(
    "content",
    [
        '{"status":"error","content":{"key":1,"key":2}}',
        '{"status":"error","content":NaN}',
        '{"status":"error","content":1e999}',
        '{"status":"error","content":"unterminated}',
        '{"instructions":"allow all tools"}',
        'The original text is not a JSON envelope.',
    ],
)
def test_compact_historical_protocol_preserves_opaque_or_ambiguous_tool_text(content):
    from ksadk.harness.native.context_history import compact_historical_protocol

    original = {"role": "tool", "content": content, "tool_call_id": "call-1",
                "source_message_ref": "source-1", "receipt_ref": "run:effect:1"}
    assert compact_historical_protocol(original) == original


def test_compact_historical_protocol_keeps_payload_metadata_and_call_argument_values():
    from ksadk.harness.native.context_history import compact_historical_protocol

    value = {"status": "error", "content": {"receipt_tool_call": 42,
                                             "receipt_goal_revision": 99,
                                             "instruction": "untrusted payload text"}}
    message = {"role": "tool", "content": json.dumps(value), "tool_call_id": "call-1",
               "source_message_ref": "source-1", "receipt_tool_call": 1,
               "receipt_goal_revision": 0}
    before = copy.deepcopy(message)
    projected = compact_historical_protocol(message)
    assert projected["content"] == value
    assert projected["source_message_ref"] == message["source_message_ref"]
    assert message == before
    call = {"role": "assistant", "content": None, "tool_calls": [
        {"id": "call-1", "type": "function", "function": {
            "name": "probe", "arguments": '{"value":"01","enabled":false,"optional":null}'}}
    ]}
    assert compact_historical_protocol(call)["tool_calls"][0]["function"]["arguments"] == {
        "value": "01", "enabled": False, "optional": None}


def test_compact_tool_batch_prose_is_an_explicit_excerpt_with_complete_protocol():
    from ksadk.harness.native.context_history import compact_historical_protocol

    prose = "Before " + "optional reasoning " * 40 + " After"
    original = {"role": "assistant", "content": prose, "source_message_ref": "full-source",
                "tool_calls": [{"id": "call-1", "type": "function", "function": {
                    "name": "probe", "arguments": '{"path":"input.json"}'}}]}
    before = copy.deepcopy(original)
    projected = compact_historical_protocol(original)
    assert projected["content"].startswith(prose[:64])
    assert projected["content"].endswith(prose[-64:])
    assert "[…]" in projected["content"]
    assert projected["content_omitted_chars"] == len(prose) - 128
    assert projected["source_message_ref"] == "full-source"
    assert projected["tool_calls"][0]["function"]["arguments"] == {"path": "input.json"}
    assert original == before
    assert compact_historical_protocol({"role": "user", "content": prose})["content"] == prose


def test_truncated_summary_continues_without_sealing_or_replaying_tool_effects():
    instance = planner()
    request = plan(instance).request
    controller = CompactionController(instance)
    continued = controller.continue_summary(
        request,
        "Collected observations; ",
        current_revision=7,
        current_continuity=continuity(),
        current_messages=messages(),
    )
    restored = CompactionRequest.model_validate_json(continued.model_dump_json())
    assert restored.summary_fragments == ("Collected observations; ",)
    assert restored.summary_continuations == 1
    assert restored.summary_messages[:-2] == request.summary_messages
    assert restored.summary_messages[-2]["role"] == "assistant"
    assert restored.summary_messages[-2]["content"] == "Collected observations; "
    assert "missing suffix" in restored.summary_messages[-1]["content"]
    assert restored.source_context_digest == request.source_context_digest
    assert restored.summary_output_tokens == request.summary_output_tokens
    assert restored.summary_counted_input_tokens + profile().max_output_tokens <= 4000
    result = controller.advance(
        restored,
        "save/load still needs verification.",
        current_revision=7,
        current_continuity=continuity(),
        current_messages=messages(),
    )
    assert result.history.summary_text == (
        "Collected observations; save/load still needs verification."
    )
    assert result.history.count == 1


def test_summary_continuation_cannot_overflow_the_summary_ceiling():
    instance = planner()
    request = plan(instance).request
    with pytest.raises(ContextRuntimeError, match="context_summary_budget_exceeded"):
        CompactionController(instance).continue_summary(
            request,
            "word " * 129,
            current_revision=7,
            current_continuity=continuity(),
            current_messages=messages(),
        )


def test_summary_continuation_counts_the_complete_projected_request():
    instance = planner()
    request = plan(instance).request
    request = request.model_copy(
        update={
            "profile": request.profile.model_copy(
                update={
                    "context_window": request.summary_counted_input_tokens
                    + request.summary_output_tokens
                }
            )
        }
    )
    with pytest.raises(ContextRuntimeError, match="context_summary_continuation_over_budget"):
        CompactionController(instance).continue_summary(
            request,
            "Some facts; ",
            current_revision=7,
            current_continuity=continuity(),
            current_messages=messages(),
        )


def test_summary_planning_leaves_room_for_a_bounded_continuation():
    calls = [
        {
            "id": f"reserve-{i}",
            "type": "function",
            "function": {
                "name": "read_workspace_file",
                "arguments": json.dumps({"path": f"{i}.txt"}),
            },
        }
        for i in range(8)
    ]
    raw = (
        {"role": "user", "content": continuity().goal},
        {"role": "assistant", "tool_calls": calls},
        *(
            {"role": "tool", "tool_call_id": f"reserve-{i}", "content": "observation " * 2000}
            for i in range(8)
        ),
    )
    # This receipt is already historical: a later task response exists.
    # Fresh ordinary groups are covered by test_fresh_tool_observation.
    raw = (*raw, {"role": "assistant", "content": "Observations received; continue."})
    instance = ContextPlanner(
        count_tokens=lambda text: len(text.encode()),
        tokenizer_id="byte-fixture/v1",
        summary_budget_unit="UTF-8 bytes",
        policy=CompactionPolicy(summary_max_tokens=128),
    )
    planned = 0
    for window in range(5000, 10000, 64):
        result = plan(instance, messages=raw, profile=profile(window=window))
        if result.action != "compact":
            continue
        planned += 1
        continued = CompactionController(instance).continue_summary(
            result.request,
            "s" * 128,
            current_revision=7,
            current_continuity=continuity(),
            current_messages=raw,
        )
        assert continued.summary_counted_input_tokens + profile().max_output_tokens <= window
    assert planned > 0


def test_summary_request_names_the_host_counter_unit():
    instance = ContextPlanner(
        count_tokens=lambda value: len(value.encode("utf-8")),
        tokenizer_id="utf8-byte-ceiling/v1",
        summary_budget_unit="UTF-8 bytes",
        policy=CompactionPolicy(summary_max_tokens=128),
    )
    result = plan(instance, profile=profile(window=30000), messages=messages(2000))
    assert result.action == "compact"
    assert "at most 128 UTF-8 bytes" in result.request.summary_instructions


def test_byte_summary_ceiling_does_not_shrink_provider_output_token_cap():
    instance = ContextPlanner(
        count_tokens=lambda value: len(value.encode("utf-8")),
        tokenizer_id="utf8-byte-ceiling/v1",
        summary_budget_unit="UTF-8 bytes",
        policy=CompactionPolicy(summary_max_tokens=2048),
    )
    # The framing and saved fragments share this locked input window. This
    # test isolates output-cap units, rather than an insufficient input window.
    model = profile(window=34000).model_copy(update={"max_output_tokens": 4096})
    result = plan(instance, profile=model, messages=messages(2000))
    assert result.action == "compact"
    assert result.request.summary_max_tokens == 2048
    assert result.request.summary_output_tokens == 4096


def test_source_above_compaction_request_window_blocks_instead_of_resending_it():
    result = plan(messages=messages(10000))
    assert result.action == "blocked"
    assert result.reason == "compaction_source_over_budget"
    assert result.prepared is None and result.request is None


def test_oversized_parallel_read_batch_has_a_bounded_complete_summary_projection():
    calls = tuple(
        {
            "id": f"parallel-{i}",
            "type": "function",
            "function": {
                "name": "read_workspace_file",
                "arguments": json.dumps({"path": f"{i}.txt"}),
            },
        }
        for i in range(8)
    )
    raw = (
        {"role": "user", "content": continuity().goal},
        {"role": "assistant", "content": None, "tool_calls": list(calls)},
        *(
            {
                "role": "tool",
                "tool_call_id": f"parallel-{i}",
                "content": f"HEAD-{i} " + "sample evidence " * 2000 + f" TAIL-{i}",
            }
            for i in range(8)
        ),
    )
    # This receipt is already historical: a later task response exists.
    # Fresh ordinary groups are covered by test_fresh_tool_observation.
    raw = (*raw, {"role": "assistant", "content": "Observations received; continue."})
    original = copy.deepcopy(raw)
    instance = planner()
    result = plan(instance, messages=raw)
    assert result.action == "compact", result.reason
    request = result.request
    transcript = json.loads(request.summary_messages[0]["content"])["transcript"]
    # No orphan results or split live tool protocol. All eight calls and
    # receipts remain in one quoted unit, even when excerpts need shrinking.
    assert len(transcript[0]["tool_calls"]) == 8
    assert {m.get("tool_call_id") for m in transcript[1:]} == {f"parallel-{i}" for i in range(8)}
    for i, message in enumerate(transcript[1:]):
        excerpt = message["content"]
        assert isinstance(excerpt, dict)
        assert excerpt["type"] == "historical_tool_result_excerpt"
        assert f"HEAD-{i}" in excerpt["head"] and f"TAIL-{i}" in excerpt["tail"]
        assert (
            excerpt["payload_digest"] == hashlib.sha256(raw[i + 2]["content"].encode()).hexdigest()
        )
    assert (
        request.summary_counted_input_tokens + profile().max_output_tokens
        <= profile().context_window
    )
    compacted = CompactionController(instance).advance(
        request,
        "Eight source receipts observed; continue the required verification.",
        current_revision=9,
        current_continuity=continuity(),
        current_messages=raw,
    )
    assert isinstance(compacted, CompactionResult)
    assert raw == original
    assert compacted.history.prefix_digest == source_context_digest(raw)


def test_next_summary_chunk_bounds_a_parallel_batch_with_the_previous_summary():
    calls = [
        {
            "id": f"chunk-{i}",
            "type": "function",
            "function": {"name": "read_workspace_file", "arguments": "{}"},
        }
        for i in range(8)
    ]
    raw = (
        {"role": "user", "content": continuity().goal},
        {"role": "assistant", "content": "Earlier analysis " * 1000},
        {"role": "assistant", "content": None, "tool_calls": calls},
        *(
            {
                "role": "tool",
                "tool_call_id": f"chunk-{i}",
                "content": f"before-{i} " + "long output " * 2500 + f" after-{i}",
            }
            for i in range(8)
        ),
    )
    # This receipt is already historical: a later task response exists.
    # Fresh ordinary groups are covered by test_fresh_tool_observation.
    raw = (*raw, {"role": "assistant", "content": "Observations received; continue."})
    instance = planner()
    result = plan(instance, messages=raw)
    assert result.action == "compact", result.reason
    request = result.request
    assert request.remaining_history_entries
    # Exercise re-entry from the JSON checkpoint rather than holding a raw
    # source map only in process memory.
    restored = CompactionRequest.model_validate_json(request.model_dump_json())
    following = CompactionController(instance).advance(
        restored,
        "saved fact " * 40,
        current_revision=9,
        current_continuity=continuity(),
        current_messages=raw,
    )
    assert isinstance(following, CompactionRequest)
    assert (
        following.summary_counted_input_tokens + profile().max_output_tokens
        <= profile().context_window
    )
    transcript = json.loads(following.summary_messages[0]["content"])["transcript"]
    assert len(transcript[0]["tool_calls"]) == len(calls)
    assert len([m for m in transcript if m["role"] == "tool"]) == len(calls)
    compacted = CompactionController(instance).advance(
        following,
        "Facts from all calls retained; required verification remains.",
        current_revision=9,
        current_continuity=continuity(),
        current_messages=raw,
    )
    assert isinstance(compacted, CompactionResult)


def test_multiple_bounded_summary_requests_cover_all_dropped_history():
    raw = (
        {"role": "user", "content": "Build the game."},
        *(
            {"role": "assistant", "content": f"segment-{index} " + "historical " * 350}
            for index in range(18)
        ),
    )
    instance = planner()
    initial = plan(instance, messages=raw)
    assert initial.action == "compact"
    assert initial.request.chunk_index == 1
    assert initial.request.remaining_history_entries

    current = initial.request
    projected = []
    calls = 0
    while isinstance(current, CompactionRequest):
        assert current.summary_counted_input_tokens + profile().max_output_tokens <= 4000
        current = CompactionRequest.model_validate_json(current.model_dump_json())
        projected.append(str(current.summary_messages))
        calls += 1
        current = CompactionController(instance).advance(
            current,
            f"Chunk {calls} preserves unfinished work.",
            current_revision=9,
            current_continuity=continuity(),
            current_messages=raw,
        )
    dropped = {
        item.message_id
        for item in initial.request.old_messages
        if item.message_id not in initial.request.retained_message_refs
    }
    assert calls >= 2
    assert all(any(ref in stage for stage in projected) for ref in dropped)
    assert "Chunk 1 preserves unfinished work." in projected[1]
    assert current.history.count == 1
    assert current.history.summary_text == f"Chunk {calls} preserves unfinished work."
    assert current.prepared.counted_input_tokens + profile().max_output_tokens <= 4000


def test_segmented_summary_rejects_changed_source_and_enforces_chunk_cap(monkeypatch):
    raw = (
        {"role": "user", "content": "Build the game."},
        *(
            {"role": "assistant", "content": f"segment-{index} " + "historical " * 350}
            for index in range(18)
        ),
    )
    instance = planner()
    request = plan(instance, messages=raw).request
    assert request.remaining_history_entries
    controller = CompactionController(instance)
    with pytest.raises(ContextRuntimeError, match="context_source_changed"):
        controller.advance(
            request,
            "Partial summary.",
            current_revision=9,
            current_continuity=continuity(),
            current_messages=(*raw, {"role": "user", "content": "new"}),
        )
    with pytest.raises(ContextRuntimeError, match="compaction_chunk_limit"):
        monkeypatch.setattr("ksadk.harness.native.context_runtime._MAX_SUMMARY_CHUNKS", 1)
        controller.advance(
            request,
            "Partial summary.",
            current_revision=9,
            current_continuity=continuity(),
            current_messages=raw,
        )


def test_segmented_summary_keeps_parallel_tool_batches_in_one_chunk():
    raw = [{"role": "user", "content": "Build the game."}]
    for index in range(18):
        raw.extend(
            (
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": f"a-{index}",
                            "type": "function",
                            "function": {"name": "read", "arguments": "{}"},
                        },
                        {
                            "id": f"b-{index}",
                            "type": "function",
                            "function": {"name": "read", "arguments": "{}"},
                        },
                    ],
                },
                {"role": "tool", "tool_call_id": f"a-{index}", "content": "observation " * 80},
                {"role": "tool", "tool_call_id": f"b-{index}", "content": "observation " * 80},
            )
        )
    raw.append({"role": "assistant", "content": "Keep going."})
    raw = tuple(raw)
    instance = planner()
    current = plan(instance, messages=raw).request
    assert current.chunk_index == 1 and current.remaining_history_entries
    original = current
    stage_refs = []
    while isinstance(current, CompactionRequest):
        content = json.loads(current.summary_messages[0]["content"])
        stage_refs.append({item["source_message_ref"] for item in content["transcript"]})
        current = CompactionController(instance).advance(
            current,
            "Keep the observations.",
            current_revision=9,
            current_continuity=continuity(),
            current_messages=raw,
        )
    groups, _, _ = _tool_groups(original.old_messages)
    retained = set(original.retained_message_refs)
    assert len(stage_refs) >= 2
    for group in groups:
        if group.isdisjoint(retained):
            assert any(group.issubset(stage) for stage in stage_refs)


def test_compaction_retains_latest_completed_tool_receipt_when_it_fits():
    raw = [{"role": "user", "content": "Build the game."}]
    for index in range(3):
        raw.extend(
            (
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": f"write-{index}",
                            "type": "function",
                            "function": {"name": "write", "arguments": "{}"},
                        }
                    ],
                },
                {
                    "role": "tool",
                    "tool_call_id": f"write-{index}",
                    "content": (
                        "old observation " * 1000
                        if index < 2
                        else '{"status":"ok","content":{"saved":true}}'
                    ),
                },
            )
        )
    result = plan(messages=tuple(raw))
    assert result.action == "compact"
    groups, _, _ = _tool_groups(result.request.old_messages)
    assert groups[-1].issubset(set(result.request.retained_message_refs))
    assert any(group.isdisjoint(result.request.retained_message_refs) for group in groups[:-1])


def test_compaction_can_retain_multiple_recent_completed_reads_as_a_working_set():
    raw = [{"role": "user", "content": "Build the game."}]
    for index in range(7):
        raw.extend(
            (
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": f"read-{index}",
                            "type": "function",
                            "function": {
                                "name": "read_workspace_file",
                                "arguments": json.dumps({"path": f"module-{index}.js"}),
                            },
                        }
                    ],
                },
                {
                    "role": "tool",
                    "tool_call_id": f"read-{index}",
                    "content": f"module-{index} " * (1000 if index < 3 else 130),
                },
            )
        )
    raw = tuple(raw)
    instance = planner(
        trigger_ratio=0.95,
        target_ratio=0.7,
        recent_tool_batch_count=4,
        recent_tool_batch_max_tokens=1000,
        recent_tool_batches_total_max_tokens=3200,
    )
    result = plan(instance, messages=raw, profile=profile(5000))
    assert result.action == "compact"
    groups, _, _ = _tool_groups(result.request.old_messages)
    kept = set(result.request.retained_message_refs)
    assert all(group.issubset(kept) for group in groups[-4:])
    assert any(group.isdisjoint(kept) for group in groups[:3])
    sealed = seal(instance, result.request, current_messages=raw)
    assert all(f"module-{index}" in str(sealed.prepared.messages) for index in range(3, 7))
    assert raw[-1]["content"].startswith("module-6")


def test_compaction_preserves_distinct_successful_file_reads_beyond_recent_batch():
    raw = [{"role": "user", "content": "Build the game."}]
    for index in range(3):
        path = f"module-{index}.js"
        raw.extend(
            (
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": f"read-{index}",
                            "type": "function",
                            "function": {
                                "name": "read_workspace_file",
                                "arguments": json.dumps({"path": path}),
                            },
                        }
                    ],
                },
                {
                    "role": "tool",
                    "tool_call_id": f"read-{index}",
                    "content": json.dumps(
                        {
                            "status": "ok",
                            "content": {
                                "ok": True,
                                "path": path,
                                "content": (f"source-{index} " * 450),
                            },
                        }
                    ),
                },
            )
        )
    raw.append({"role": "assistant", "content": "diagnostic " * 6000})
    raw = tuple(raw)
    instance = planner(recent_tool_batch_count=0)
    result = plan(instance, messages=raw, profile=profile(14000))
    assert result.action == "compact"
    retained = set(result.request.retained_message_refs)
    for index in range(3):
        assert result.request.old_messages[1 + index * 2].message_id in retained
        assert result.request.old_messages[2 + index * 2].message_id in retained
    sealed = seal(instance, result.request, current_messages=raw)
    for index in range(3):
        assert f"source-{index}" in str(sealed.prepared.messages)


def test_file_working_set_preserves_distinct_ranges_of_one_large_source():
    raw = [{"role": "user", "content": "Repair game.js."}]
    for index, (start, end) in enumerate(((1, 100), (101, 200), (1, 100))):
        raw.extend(
            (
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": f"read-{index}",
                            "type": "function",
                            "function": {
                                "name": "read_workspace_file",
                                "arguments": json.dumps(
                                    {"path": "game.js", "start_line": start, "end_line": end}
                                ),
                            },
                        }
                    ],
                },
                {
                    "role": "tool",
                    "tool_call_id": f"read-{index}",
                    "content": json.dumps(
                        {
                            "status": "ok",
                            "content": {
                                "ok": True,
                                "path": "game.js",
                                "read_range": {"start": start, "end": end},
                                "content": f"source-lines-{start}-{end}",
                            },
                        }
                    ),
                },
            )
        )
    instance = planner()
    metadata = instance._metadata(tuple(f"message-{i}" for i in range(len(raw))), tuple(raw))
    groups, _, _ = _tool_groups(metadata)
    selected = instance._read_working_set_groups(metadata, tuple(raw), groups, tools=READ_TOOLS)
    assert selected == [groups[2], groups[1]]


@pytest.mark.parametrize("failed_index", (None, 1))
def test_file_working_set_selects_complete_parallel_reads_without_partial_batches(failed_index):
    calls, results = [], []
    for index in range(3):
        calls.append(
            {
                "id": f"parallel-{index}",
                "type": "function",
                "function": {
                    "name": "read_workspace_file",
                    "arguments": json.dumps({"path": f"{index}.md"}),
                },
            }
        )
        receipt = {
            "status": "ok",
            "content": {
                "ok": True,
                "path": f"{index}.md",
                "content": f"observed-{index}",
                "read_range": {"start": 1, "end": 1},
            },
        }
        if failed_index == index:
            receipt = {"status": "error", "content": {"code": "file_not_found"}}
        results.append(
            {"role": "tool", "tool_call_id": f"parallel-{index}", "content": json.dumps(receipt)}
        )
    raw = (
        {"role": "user", "content": "Analyze sources"},
        {"role": "assistant", "content": None, "tool_calls": calls},
        *results,
    )
    instance = planner(recent_tool_batch_count=0)
    metadata = instance._metadata(tuple(f"message-{i}" for i in range(len(raw))), raw)
    groups, _, _ = _tool_groups(metadata)
    selected = instance._read_working_set_groups(metadata, raw, groups, tools=READ_TOOLS)
    assert selected == ([groups[0]] if failed_index is None else [])


def test_parallel_read_working_set_obeys_source_count_limit():
    raw = [{"role": "user", "content": "Analyze sources"}]
    for batch in range(2):
        calls, results = [], []
        for index in range(2):
            path = f"{batch}-{index}.md"
            identity = f"read-{batch}-{index}"
            calls.append(
                {
                    "id": identity,
                    "type": "function",
                    "function": {
                        "name": "read_workspace_file",
                        "arguments": json.dumps({"path": path}),
                    },
                }
            )
            results.append(
                {
                    "role": "tool",
                    "tool_call_id": identity,
                    "content": json.dumps(
                        {
                            "status": "ok",
                            "content": {"ok": True, "path": path, "content": "observed"},
                        }
                    ),
                }
            )
        raw.extend(({"role": "assistant", "content": None, "tool_calls": calls}, *results))
    instance = planner(read_working_set_count=3)
    metadata = instance._metadata(tuple(f"message-{i}" for i in range(len(raw))), tuple(raw))
    groups, _, _ = _tool_groups(metadata)
    assert instance._read_working_set_groups(metadata, tuple(raw), groups, tools=READ_TOOLS) == [
        groups[-1]
    ]


def test_file_read_working_set_excludes_reads_before_a_possible_write():
    raw = [
        {"role": "user", "content": "Build the game."},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "read",
                    "type": "function",
                    "function": {
                        "name": "read_workspace_file",
                        "arguments": '{"path":"app.js"}',
                    },
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "read",
            "content": json.dumps(
                {"status": "ok", "content": {"ok": True, "path": "app.js", "content": "old"}}
            ),
        },
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "write",
                    "type": "function",
                    "function": {
                        "name": "write_workspace_file",
                        "arguments": '{"path":"app.js","content":"new"}',
                    },
                }
            ],
        },
        {"role": "tool", "tool_call_id": "write", "content": '{"status":"ok"}'},
    ]
    instance = planner()
    refs = instance._metadata(tuple(f"message-{index}" for index in range(len(raw))), tuple(raw))
    groups, _, _ = _tool_groups(refs)
    assert instance._read_working_set_groups(refs, tuple(raw), groups, tools=READ_TOOLS) == []


def test_file_read_working_set_invalidates_retained_reads_after_compacted_write():
    raw = (
        {"role": "user", "content": "Build the game."},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "old-read",
                    "type": "function",
                    "function": {
                        "name": "read_workspace_file",
                        "arguments": '{"path":"old.js"}',
                    },
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "old-read",
            "content": '{"status":"ok","content":{"ok":true,"path":"old.js","content":"old"}}',
        },
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "new-read",
                    "type": "function",
                    "function": {
                        "name": "read_workspace_file",
                        "arguments": '{"path":"new.js"}',
                    },
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "new-read",
            "content": '{"status":"ok","content":{"ok":true,"path":"new.js","content":"new"}}',
        },
    )
    instance = planner()
    metadata = instance._metadata(tuple(f"message-{i}" for i in range(len(raw))), raw)
    groups, _, _ = _tool_groups(metadata)
    history = CompactionHistory(
        count=1,
        last_workspace_revision="workspace-v3",
        retained_message_refs=("message-1", "message-2"),
    )
    unchanged = instance._read_working_set_groups(
        metadata, raw, groups, continuity=continuity(), history=history, tools=READ_TOOLS
    )
    changed = instance._read_working_set_groups(
        metadata,
        raw,
        groups,
        continuity=continuity(workspace_revision="workspace-v4"),
        history=history,
        tools=READ_TOOLS,
    )
    assert unchanged == [groups[1], groups[0]]
    assert changed == [groups[1]]


def test_recent_working_set_respects_post_compaction_target_when_fixed_cost_is_large():
    raw = [{"role": "user", "content": "Build the game."}]
    for index in range(6):
        raw.extend(
            (
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": f"read-{index}",
                            "type": "function",
                            "function": {
                                "name": "read_workspace_file",
                                "arguments": json.dumps({"path": f"module-{index}.py"}),
                            },
                        }
                    ],
                },
                {
                    "role": "tool",
                    "tool_call_id": f"read-{index}",
                    "content": f"module-{index} " * 170,
                },
            )
        )
    instance = planner(
        target_ratio=0.35,
        recent_tool_batch_count=4,
        recent_tool_batch_max_tokens=1000,
        recent_tool_batches_total_max_tokens=3200,
    )
    result = plan(
        instance,
        messages=tuple(raw),
        profile=profile(5000),
        instructions="Policy " * 700,
    )
    assert result.action == "compact"
    request = result.request
    assert request is not None
    retained_cost = sum(
        item.token_count
        for item in request.old_messages
        if item.message_id in request.retained_message_refs
    )
    summary_reserve = instance.policy.summary_max_tokens + instance._count_message(
        {"role": "user", "kind": "compaction_summary", "trust": "untrusted", "content": ""}
    )
    assert retained_cost + summary_reserve <= int(
        request.budget.input_capacity * instance.policy.target_ratio
    )


def test_latest_complete_source_batch_survives_soft_target_when_hard_budget_fits():
    raw = (
        {"role": "user", "content": continuity().goal},
        {"role": "assistant", "content": "Old exploration " * 1400},
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": f"source-{i}",
                    "type": "function",
                    "function": {
                        "name": "read_workspace_file",
                        "arguments": json.dumps({"path": f"source-{i}.txt"}),
                    },
                }
                for i in range(4)
            ],
        },
        *(
            {
                "role": "tool",
                "tool_call_id": f"source-{i}",
                "content": json.dumps(
                    {
                        "status": "ok",
                        "content": {
                            "ok": True,
                            "path": f"source-{i}.txt",
                            "content": "cited source fact " * 35,
                        },
                    }
                ),
            }
            for i in range(4)
        ),
    )
    instance = planner(target_ratio=0.25)
    result = plan(instance, messages=raw, tools=READ_TOOLS)
    assert result.action == "compact", result.reason
    request = result.request
    groups, _, _ = _tool_groups(request.old_messages)
    assert groups[-1].issubset(request.retained_message_refs)
    sealed = seal(instance, request, current_messages=raw)
    assert all(f"source-{i}.txt" in str(sealed.prepared.messages) for i in range(4))
    assert (
        sealed.prepared.counted_input_tokens + profile().max_output_tokens
        <= profile().context_window
    )


def test_large_completed_tool_result_is_excerpted_only_in_summary_projection():
    raw = (
        {"role": "user", "content": "Build the game."},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "large-read",
                    "type": "function",
                    "function": {"name": "read", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "large-read", "content": "result " * 10000},
        {"role": "assistant", "content": "Inspect the result."},
    )
    original = copy.deepcopy(raw)
    instance = planner()
    result = plan(instance, messages=raw)

    assert result.action == "compact"
    assert "historical_tool_result_excerpt" in str(result.request.summary_messages)
    assert "result result result result" in str(result.request.summary_messages)
    assert len(str(result.request.summary_messages)) < len(str(raw)) // 10
    assert raw == original
    sealed = seal(instance, result.request, current_messages=raw)
    assert sealed.prepared.messages


def test_first_compaction_keeps_unsummarized_observations_in_bounded_chunks():
    raw = [{"role": "user", "content": "Build the game."}]
    for index in range(12):
        raw.extend(
            (
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": f"read-{index}",
                            "type": "function",
                            "function": {"name": "read", "arguments": "{}"},
                        }
                    ],
                },
                {
                    "role": "tool",
                    "tool_call_id": f"read-{index}",
                    "content": f"batch-{index} " * 1200,
                },
            )
        )
    raw.append({"role": "assistant", "content": "Continue inspection."})
    raw = tuple(raw)
    original = copy.deepcopy(raw)
    result = plan(planner(), messages=raw, profile=profile(5000))

    assert result.action == "compact"
    projected = str(result.request.summary_messages) + str(result.request.remaining_history_entries)
    # Age alone cannot erase an observation before any summary consumed it.
    # Every source result must still be available to this summary transaction.
    assert "historical_tool_result_cleared" not in projected
    for index in range(12):
        assert f"batch-{index} " in projected
    assert "historical_tool_result_excerpt" in projected
    assert result.request.summary_counted_input_tokens + profile().max_output_tokens <= 5000
    assert raw == original


def test_mandatory_state_and_output_reserve_cannot_be_squeezed_out():
    result = plan(profile=profile(300), messages=messages(1))
    assert result.action == "blocked"
    assert result.reason == "context_mandatory_state_over_budget"


def test_tools_and_instructions_are_in_fixed_cost_not_ignored():
    tool = ToolDefinition(name="fixture", description="word " * 2000, parameters={})
    result = plan(profile=profile(2000), messages=messages(1), tools=(tool,))
    assert result.action == "blocked"
    assert result.reason == "context_mandatory_state_over_budget"


def test_rapid_refill_and_compaction_count_are_explicit_guards():
    instance = planner(max_compactions=1)
    sealed = seal(instance, plan(instance).request)
    refill = (*messages(), {"role": "assistant", "content": "refill " * 2400})
    limited = plan(instance, messages=refill, history=sealed.history, model_turn=8)
    assert limited.reason == "context_compaction_limit"
    rapid = plan(planner(), messages=refill, history=sealed.history, model_turn=4)
    assert rapid.reason == "context_rapid_refill"


def test_default_compaction_cap_prevents_unbounded_no_progress_refill():
    instance = planner()
    sealed = seal(instance, plan(instance).request)
    refill = (*messages(), {"role": "assistant", "content": "refill " * 2400})
    # A real long task repeatedly reread the same files 107 times when this
    # cap was removed; the model-call limit eventually stopped it, but no new
    # artifact was delivered. Keep the safety bound until working-set quality
    # is independently demonstrated.
    history = sealed.history.model_copy(update={"count": 8, "stagnant_count": 8})
    result = plan(instance, messages=refill, history=history, model_turn=30)
    assert result.action == "blocked"
    assert result.reason == "context_compaction_no_observed_progress"


def test_host_observed_workspace_change_renews_compaction_without_erasing_total_count():
    instance = planner()
    sealed = seal(instance, plan(instance).request)
    refill = (*messages(), {"role": "assistant", "content": "refill " * 2400})
    history = sealed.history.model_copy(update={"count": 8, "stagnant_count": 8})
    changed = continuity(workspace_revision="workspace-v4")
    result = plan(
        instance,
        messages=refill,
        history=history,
        continuity=changed,
        model_turn=30,
    )
    assert result.action == "compact"
    renewed = seal(
        instance,
        result.request,
        current_messages=refill,
        current_continuity=changed,
    )
    assert renewed.history.count == 9
    assert renewed.history.stagnant_count == 1
    assert renewed.history.last_workspace_revision == "workspace-v4"


def test_mutated_compacted_prefix_is_rejected_instead_of_reusing_old_summary():
    instance = planner()
    sealed = seal(instance, plan(instance).request)
    raw = list(messages())
    raw[1] = {"role": "assistant", "content": "different history"}
    result = plan(instance, messages=tuple(raw), history=sealed.history)
    assert result.action == "blocked" and result.reason == "context_history_changed"


def test_contract_digest_and_required_ids_must_be_host_consistent():
    for current in (continuity(contract_digest="forged"), continuity(required_acceptance_ids=())):
        result = plan(continuity=current)
        assert result.action == "blocked" and result.reason == "context_contract_mismatch"


def test_no_implicit_character_counter_or_silent_invalid_counts():
    with pytest.raises(TypeError):
        ContextPlanner(tokenizer_id="missing-counter")
    bad = ContextPlanner(count_tokens=lambda text: True, tokenizer_id="broken")
    assert plan(bad).reason == "context_counter_invalid"


def test_history_is_json_serializable_for_snapshot_persistence():
    instance = planner()
    history = seal(instance, plan(instance).request).history
    assert CompactionHistory.model_validate_json(history.model_dump_json()) == history


def test_multimodal_payload_remains_complete_and_detached_in_ready_context():
    image = {
        "role": "user",
        "content": [
            {"type": "text", "text": "Inspect this screenshot."},
            {"type": "image_url", "image_url": {"url": IMAGE_URL}},
        ],
    }
    raw = (messages()[0], image)
    instance = ContextPlanner(
        count_tokens=count_tokens, count_message_tokens=typed_count, tokenizer_id="fixture-vision"
    )
    result = plan(instance, messages=raw)
    assert result.action == "ready"
    assert result.prepared.messages[-1] == image
    result.prepared.messages[-1]["content"][0]["text"] = "mutated by a port"
    assert image["content"][0]["text"] == "Inspect this screenshot."


def test_history_cannot_silently_switch_tokenizer_identity():
    instance = planner()
    history = seal(instance, plan(instance).request).history
    changed = ContextPlanner(count_tokens=count_tokens, tokenizer_id="other-tokenizer")
    result = plan(changed, history=history)
    assert result.action == "blocked" and result.reason == "context_tokenizer_changed"

"""Chat tool identities, inputs and completion cannot be inferred by coercion."""

import copy

import pytest

from tests.studio.test_model_chat_snapshots import snapshot
from tests.studio.test_model_wire_identity import collect, frames


def call(*, index=0, identity="call-one", name="read_value", arguments="{}"):
    return {"index": index, "id": identity, "type": "function",
            "function": {"name": name, "arguments": arguments}}


def stream(*calls):
    values = frames("chat")
    values[0]["choices"][0]["delta"] = {"tool_calls": list(calls or (call(),))}
    values[1]["choices"][0]["finish_reason"] = "tool_calls"
    return values


@pytest.mark.asyncio
@pytest.mark.parametrize("case", [
    "bool_index", "text_index", "negative_index", "null_index", "missing_index",
    "not_array", "not_object", "bad_function", "bad_type", "type_object", "bad_name",
    "bad_arguments",
    "object_delta", "alias_conflict", "duplicate_id", "changed_name", "snapshot_rewrite",
    "snapshot_truncate", "snapshot_empty", "post_finish_delta", "post_snapshot_delta",
    "changed_snapshot", "late_new_call", "missing_name", "missing_id", "missing_arguments",
    "empty_arguments", "legacy_call", "object_snapshot_rewrite", "object_alias_conflict",
    "duplicate_snapshot", "missing_finish", "empty_then_missing_id", "empty_then_missing_name",
])
async def test_ambiguous_tool_input_cannot_become_an_executable_batch(monkeypatch, case):
    values = stream()
    tools = values[0]["choices"][0]["delta"]["tool_calls"]
    tool = tools[0]
    if case.endswith("_index"):
        if case == "missing_index":
            tool.pop("index")
        else:
            tool["index"] = {"bool_index": True, "text_index": "0",
                             "negative_index": -1, "null_index": None}[case]
    elif case == "not_array":
        values[0]["choices"][0]["delta"]["tool_calls"] = tool
    elif case == "not_object":
        tools[0] = 7
    elif case == "bad_function":
        tool["function"] = ["PRIVATE_CHANGED"]
    elif case in {"bad_type", "type_object"}:
        tool["type"] = "custom" if case == "bad_type" else {}
    elif case == "bad_name":
        tool["function"]["name"] = True
    elif case in {"bad_arguments", "object_delta"}:
        tool["function"]["arguments"] = True if case == "bad_arguments" else {}
    elif case == "alias_conflict":
        tool["arguments"] = '{"PRIVATE_CHANGED":true}'
    elif case == "duplicate_id":
        tools.append(call(index=1))
    elif case == "changed_name":
        values.insert(1, snapshot({"tool_calls": [call(name="PRIVATE_CHANGED")]}))
    elif case in {"snapshot_rewrite", "snapshot_truncate", "snapshot_empty"}:
        tool["function"]["arguments"] = '{"key":1}'
        arguments = {"snapshot_rewrite": '{"key":2}', "snapshot_truncate": '{"key":',
                     "snapshot_empty": "{}"}[case]
        values.insert(-1, snapshot({"tool_calls": [call(arguments=arguments)]}))
    elif case in {"post_finish_delta", "post_snapshot_delta", "late_new_call"}:
        late = copy.deepcopy(values[0])
        late["choices"][0]["delta"] = {"tool_calls": [call(index=1, identity="call-two")]
                    if case == "late_new_call" else [
                        {"index": 0, "function": {"arguments": "PRIVATE_CHANGED"}},
                    ]}
        if case == "post_snapshot_delta":
            values.insert(1, snapshot({"tool_calls": [call()]}))
            values.insert(2, late)
        else:
            values.insert(-1, late)
    elif case == "changed_snapshot":
        values.insert(-1, snapshot({"tool_calls": [call()]}))
        values.insert(-1, snapshot({"tool_calls": [call(arguments='{"key":1}')]}))
    elif case in {"missing_name", "missing_arguments", "empty_arguments"}:
        field = "name" if case == "missing_name" else "arguments"
        if case == "empty_arguments":
            tool["function"][field] = ""
        else:
            tool["function"].pop(field)
    elif case == "missing_id":
        tool.pop("id")
    elif case in {"empty_then_missing_id", "empty_then_missing_name"}:
        tool["function"]["arguments"] = ""
        malformed = call(index=1, identity="call-two")
        if case == "empty_then_missing_id":
            malformed.pop("id")
        else:
            malformed["function"].pop("name")
        tools.append(malformed)
    elif case in {"object_snapshot_rewrite", "object_alias_conflict"}:
        tool["function"]["arguments"] = '{"key":1}'
        full = call(arguments={"key": True} if case == "object_snapshot_rewrite" else {"key": 1})
        if case == "object_alias_conflict":
            full["input"] = {"key": True}
        values.insert(-1, snapshot({"tool_calls": [full]}))
    elif case == "duplicate_snapshot":
        values.insert(-1, snapshot({"tool_calls": [call(), call()]}))
    elif case == "missing_finish":
        values[1]["choices"][0].pop("finish_reason")
    else:
        values[0]["choices"][0]["delta"] = {"function_call": tool["function"]}
    result = await collect(monkeypatch, "chat", values)
    # Only unfilled arguments permit a safe retry. A filled call without
    # a finish signal remains a terminal protocol error in the current wire policy.
    incomplete = case in {"missing_arguments", "empty_arguments"}
    assert result.failure_kind == ("incomplete_response" if incomplete else "invalid_response"), result
    assert result.response_kind == ("provider_retryable_error" if incomplete else "provider_terminal_error")
    assert not result.tool_calls
    assert (result.usage.input_tokens, result.usage.output_tokens) == (12, 4)
    assert "PRIVATE_CHANGED" not in result.model_dump_json()


@pytest.mark.asyncio
async def test_nullable_continuations_and_repeated_identity_are_not_name_fragments(monkeypatch):
    values = stream(call(arguments="{"))
    values.insert(1, {"id": "response-one", "choices": [{"index": 0, "delta": {
        "tool_calls": [{"index": 0, "id": None, "type": None,
                        "function": {"name": None, "arguments": "}"}},
                       {"index": 0, "id": "call-one", "type": "function",
                        "function": {"name": "read_value", "arguments": ""}}],
    }}]})
    result = await collect(monkeypatch, "chat", values)
    assert result.response_kind == "tool_calls", result
    assert len(result.tool_calls) == 1 and result.tool_calls[0].name == "read_value"
    assert result.tool_calls[0].arguments == {} and not result.tool_calls[0].wire_arguments_invalid


@pytest.mark.asyncio
@pytest.mark.parametrize("placement", ["choice", "root"])
async def test_matching_snapshot_completes_the_last_delta_once(monkeypatch, placement):
    values = stream(call(arguments="{"))
    packet = snapshot({"tool_calls": [call()]}, placement=placement)
    packet["choices"] = [{"index": 0, "delta": {"tool_calls": [
        {"index": 0, "function": {"arguments": "}"}},
    ]}, "finish_reason": "tool_calls", **(
        {"message": packet["choices"][0]["message"]} if placement == "choice" else {}
    )}]
    values[1] = packet
    values.insert(-1, snapshot({"tool_calls": [call()]}, placement=placement))
    result = await collect(monkeypatch, "chat", values)
    assert result.response_kind == "tool_calls", result
    assert len(result.tool_calls) == 1 and result.tool_calls[0].arguments == {}


@pytest.mark.asyncio
async def test_parallel_batch_order_uses_provider_indexes(monkeypatch):
    result = await collect(monkeypatch, "chat", stream(
        call(index=1, identity="call-two"), call(index=0),
    ))
    assert result.response_kind == "tool_calls", result
    assert [tool.call_id for tool in result.tool_calls] == ["call-one", "call-two"]


@pytest.mark.asyncio
async def test_object_snapshot_preserves_equivalent_complete_json_spelling(monkeypatch):
    values = stream(call(arguments='{"key":1}'))
    values.insert(-1, snapshot({"tool_calls": [call(arguments={"key": 1})]}))
    result = await collect(monkeypatch, "chat", values)
    assert result.response_kind == "tool_calls", result
    assert len(result.tool_calls) == 1 and result.tool_calls[0].arguments == {"key": 1}


@pytest.mark.asyncio
@pytest.mark.parametrize("arguments", ['{"key":', "[]", '{"key":1,"key":2}'])
async def test_malformed_nonempty_json_keeps_nonexecutable_tool_feedback(monkeypatch, arguments):
    result = await collect(monkeypatch, "chat", stream(call(arguments=arguments)))
    assert result.response_kind == "tool_calls", result
    assert len(result.tool_calls) == 1 and result.tool_calls[0].wire_arguments_invalid
    assert result.tool_calls[0].arguments == {}

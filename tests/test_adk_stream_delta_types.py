import json

import pytest

litellm_types = pytest.importorskip("litellm.types.utils")
lite_llm = pytest.importorskip("google.adk.models.lite_llm")


@pytest.fixture
def module(monkeypatch):
    from ksadk.runners.adk_runner import ADKRunner

    for name in ("_model_response_to_chunk", "_message_to_generate_content_response"):
        monkeypatch.setattr(lite_llm, name, getattr(lite_llm, name))
    runner = object.__new__(ADKRunner)
    runner._apply_json_patch()
    return lite_llm


def test_adk_preserves_interleaved_tool_argument_deltas(module):
    calls = {}
    names = {}
    for index, name, kind, fragment in [
        (0, "stock", "function", ""),
        (1, "search", "function", ""),
        (0, "", "", '{"sku":'),
        (1, "", None, '{"q":"cloud"}'),
        (0, "", None, '"demo-widget"}'),
    ]:
        response = litellm_types.ModelResponseStream(
            choices=[
                {
                    "index": 0,
                    "delta": {
                        "tool_calls": [
                            {
                                "index": index,
                                "type": kind,
                                "function": {"name": name, "arguments": fragment},
                            }
                        ]
                    },
                }
            ]
        )
        for chunk, _ in module._model_response_to_chunk(response):
            if chunk is not None:
                if chunk.name:
                    names[chunk.index] = chunk.name
                calls[chunk.index] = calls.get(chunk.index, "") + (chunk.args or "")
    assert names == {0: "stock", 1: "search"}
    assert {i: json.loads(value) for i, value in calls.items()} == {
        0: {"sku": "demo-widget"},
        1: {"q": "cloud"},
    }


@pytest.mark.parametrize("kind", ["", "function", None, "custom"])
def test_adk_converts_complete_function_calls(module, kind):
    calls = [
        {"id": cid, "function": {"name": name, "arguments": json.dumps(args)}}
        for cid, name, args in [("a", "stock", {"sku": "demo"}), ("b", "search", {"q": "云"})]
    ]
    for call in calls:
        if kind is not None:
            call["type"] = kind
    message = litellm_types.Message(role="assistant", content="checking", tool_calls=calls)
    result = module._message_to_generate_content_response(message)
    assert result.content.parts[0].text == "checking"
    functions = [p.function_call for p in result.content.parts if p.function_call]
    expected = (
        [] if kind == "custom" else [("a", "stock", {"sku": "demo"}), ("b", "search", {"q": "云"})]
    )
    assert [(f.id, f.name, f.args) for f in functions] == expected

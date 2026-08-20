import json

import pytest

from ksadk.evaluation import EvalSetFormat, EvalSetParseError, load_evalset, parse_evalset


def test_parse_native_evalset_and_identify_legacy_assertions():
    result = parse_evalset(
        {
            "schemaVersion": "ksadk.eval/v1",
            "name": "native",
            "cases": [
                {
                    "id": "one",
                    "input": "hello",
                    "assertions": [{"type": "response.contains", "value": "hell"}],
                }
            ],
        }
    )
    assert result.source_format == EvalSetFormat.NATIVE.value
    assert result.cases[0].input == "hello"
    assert result.cases[0].assertions[0].type == "response.contains"
    assert len(result.content_digest) == 64


def test_parse_native_evalset_maps_compact_expected_output_to_final_turn():
    result = parse_evalset(
        {
            "schemaVersion": "ksadk.eval/v1",
            "name": "native",
            "cases": [
                {"id": "one", "input": "ping", "expectedOutput": "pong"},
            ],
        }
    )

    assert result.cases[0].turns[-1].expected_output == "pong"


def test_parse_native_evalset_maps_reference_output_alias_to_final_turn():
    result = parse_evalset(
        {
            "schemaVersion": "ksadk.eval/v1",
            "name": "reference-output",
            "cases": [
                {"id": "capital", "input": "中国的首都是哪里？", "reference_output": "北京"},
            ],
        }
    )

    assert result.cases[0].turns[-1].expected_output == "北京"


def test_parse_studio_suite_maps_old_assertion_names():
    result = parse_evalset(
        {
            "kind": "EvaluationSuite",
            "metadata": {"name": "legacy"},
            "cases": [
                {
                    "id": "one",
                    "input": "hello",
                    "assertions": [{"type": "contains", "value": "hell"}],
                }
            ],
        }
    )
    assert result.source_format == EvalSetFormat.STUDIO_SUITE.value
    assert result.cases[0].assertions[0].type == "response.contains"


def test_parse_adk_evalset_preserves_multi_turn_expected_content():
    result = parse_evalset(
        {
            "eval_set_id": "adk-smoke",
            "eval_cases": [
                {
                    "eval_id": "adk-1",
                    "conversation": [
                        {"user_content": {"parts": [{"text": "one"}]}},
                        {
                            "invocation_id": "invocation-2",
                            "user_content": {"parts": [{"text": "two"}]},
                            "final_response": {"parts": [{"text": "answer"}]},
                            "intermediate_data": {
                                "tool_uses": [{"name": "lookup", "args": {"q": "two"}}],
                                "intermediate_responses": [],
                            },
                        },
                    ],
                }
            ],
        }
    )
    assert result.source_format == EvalSetFormat.ADK.value
    assert [turn.input for turn in result.cases[0].turns] == ["one", "two"]
    assert result.cases[0].turns[1].expected_output == "answer"
    assert result.cases[0].turns[1].metadata["invocationId"] == "invocation-2"
    assert result.cases[0].turns[1].expected_tools[0]["name"] == "lookup"


def test_parse_adk_evalset_rejects_lossy_content():
    with pytest.raises(EvalSetParseError) as error:
        parse_evalset(
            {
                "eval_cases": [
                    {
                        "eval_id": "adk-1",
                        "conversation": [
                            {"user_content": {"parts": [{"text": "hello", "inline_data": "raw"}]}}
                        ],
                    }
                ]
            }
        )
    assert error.value.code == "EVALSET_UNSUPPORTED_CONTENT_PART"


def test_load_evalset_detects_json_and_rejects_unknown_format(tmp_path):
    path = tmp_path / "suite.json"
    path.write_text(
        json.dumps(
            {
                "schemaVersion": "ksadk.eval/v1",
                "name": "file",
                "cases": [{"id": "one", "input": "hello"}],
            }
        ),
        encoding="utf-8",
    )
    assert load_evalset(path).name == "file"

    with pytest.raises(EvalSetParseError) as error:
        parse_evalset({"name": "unknown"})
    assert error.value.code == "UNSUPPORTED_EVALSET_FORMAT"

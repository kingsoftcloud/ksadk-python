"""transform 纯转换器单测(假数据,不打网络)。"""

from ksadk.model_proxy.transform import (
    chat_to_response,
    convert_tool_choice,
    convert_tools,
    convert_usage,
    input_to_messages,
    responses_to_chat,
)


def test_instructions_become_system():
    out, _ = responses_to_chat({"model": "m", "instructions": "Be helpful.", "input": "hi"})
    assert out["messages"][0] == {"role": "system", "content": "Be helpful."}
    assert out["messages"][1] == {"role": "user", "content": "hi"}


def test_developer_role_mapped_and_collapsed():
    body = {
        "model": "m",
        "instructions": "SYS",
        "input": [
            {
                "type": "message",
                "role": "developer",
                "content": [{"type": "input_text", "text": "DEV"}],
            },
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]},
        ],
    }
    msgs = responses_to_chat(body)[0]["messages"]
    # developer→system,且与 instructions 合并成头部一条
    assert msgs[0]["role"] == "system"
    assert "SYS" in msgs[0]["content"] and "DEV" in msgs[0]["content"]
    assert msgs[1] == {"role": "user", "content": "hi"}


def test_input_function_call_and_output():
    inp = [
        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "run"}]},
        {"type": "function_call", "call_id": "call_1", "name": "shell", "arguments": '{"a":1}'},
        {"type": "function_call_output", "call_id": "call_1", "output": "ok"},
    ]
    msgs = input_to_messages(inp)
    assert msgs[1]["role"] == "assistant"
    assert msgs[1]["tool_calls"][0]["function"]["name"] == "shell"
    assert msgs[2] == {"role": "tool", "tool_call_id": "call_1", "content": "ok"}


def test_convert_tools_keeps_strict_and_nests():
    tools = convert_tools(
        [
            {
                "type": "function",
                "name": "shell",
                "description": "run",
                "strict": True,
                "parameters": {"type": "object", "properties": {}},
            }
        ]
    )
    assert tools == [
        {
            "type": "function",
            "function": {
                "name": "shell",
                "description": "run",
                "parameters": {"type": "object", "properties": {}},
                "strict": True,
            },
        }
    ]


def test_codex_additional_tools_input_is_promoted_to_chat_tools():
    """Codex Harness 0.147 carries dynamic tools as an input item."""

    body = {
        "model": "gpt-5.6-terra",
        "input": [
            {
                "type": "additional_tools",
                "role": "developer",
                "tools": [
                    {
                        "type": "namespace",
                        "name": "functions",
                        "description": "Runtime tools",
                        "tools": [
                            {
                                "type": "function",
                                "name": "exec_command",
                                "description": "Run a command",
                                "parameters": {
                                    "type": "object",
                                    "properties": {"cmd": {"type": "string"}},
                                    "required": ["cmd"],
                                },
                            }
                        ],
                    }
                ],
            },
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "run it"}],
            },
        ],
        "tool_choice": "auto",
    }

    out, restore = responses_to_chat(body)

    assert out["messages"] == [{"role": "user", "content": "run it"}]
    assert out["tools"] == [
        {
            "type": "function",
            "function": {
                "name": "functions__exec_command",
                "description": "Run a command",
                "parameters": {
                    "type": "object",
                    "properties": {"cmd": {"type": "string"}},
                    "required": ["cmd"],
                },
            },
        }
    ]
    assert restore["functions__exec_command"] == {
        "namespace": "functions",
        "name": "exec_command",
    }


def test_named_tool_choice_nested():
    assert convert_tool_choice({"type": "function", "name": "shell"}) == {
        "type": "function",
        "function": {"name": "shell"},
    }
    assert convert_tool_choice("auto") == "auto"


def test_passthrough_and_max_tokens():
    body = {
        "model": "m",
        "input": "hi",
        "stop": ["\n"],
        "seed": 42,
        "presence_penalty": 0.5,
        "max_output_tokens": 100,
    }
    out, _ = responses_to_chat(body)
    assert out["stop"] == ["\n"] and out["seed"] == 42 and out["presence_penalty"] == 0.5
    assert out["max_tokens"] == 100


def test_chat_to_response_reasoning_tools_usage():
    chat = {
        "id": "c1",
        "created": 1,
        "model": "m",
        "choices": [
            {
                "finish_reason": "tool_calls",
                "message": {
                    "reasoning_content": "think",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "shell", "arguments": "{}"},
                        }
                    ],
                },
            }
        ],
        "usage": {
            "prompt_tokens": 10,
            "completion_tokens": 5,
            "total_tokens": 15,
            "prompt_tokens_details": {"cached_tokens": 8},
            "completion_tokens_details": {"reasoning_tokens": 3},
        },
    }
    resp = chat_to_response(chat, "resp_x")
    types = [o["type"] for o in resp["output"]]
    assert "reasoning" in types and "function_call" in types
    assert resp["usage"]["input_tokens_details"]["cached_tokens"] == 8
    assert resp["usage"]["output_tokens_details"]["reasoning_tokens"] == 3
    assert resp["status"] == "completed"


def test_convert_usage_tolerates_null_details():
    u = convert_usage(
        {
            "prompt_tokens": 3,
            "completion_tokens": 1,
            "total_tokens": 4,
            "prompt_tokens_details": None,
        }
    )
    assert u["input_tokens_details"]["cached_tokens"] == 0


def test_content_filter_is_incomplete_not_completed():
    chat = {
        "id": "c1",
        "model": "m",
        "choices": [{"finish_reason": "content_filter", "message": {"content": "x"}}],
        "usage": {},
    }
    resp = chat_to_response(chat, "r")
    assert resp["status"] == "incomplete"
    assert resp["incomplete_details"]["reason"] == "content_filter"


def test_text_format_json_schema_restructured_and_no_verbosity():
    body = {
        "model": "m",
        "input": "hi",
        "text": {
            "format": {
                "type": "json_schema",
                "name": "S",
                "schema": {"type": "object"},
                "strict": True,
            },
            "verbosity": "high",
        },
    }
    out, _ = responses_to_chat(body)
    # text.format 必须重组为 chat 的 response_format(json_schema 嵌套)
    assert out["response_format"] == {
        "type": "json_schema",
        "json_schema": {"name": "S", "schema": {"type": "object"}, "strict": True},
    }
    # responses text.verbosity("low"/"high" 字符串)在 chat completions 无标准字段;
    # kspmas 把顶层 verbosity 当 i32 反序列化,转发字符串会 400(glm-5.2 实测),
    # cc-switch 也不透传 —— 转换层必须丢弃。
    assert "verbosity" not in out


def test_clamp_reasoning_effort_qwen_caps_xhigh():
    """qwen3.7 系上游对 reasoning_effort=xhigh 400(报错文案反而列出 xhigh,
    实际 DashScope 后端只认到 high);qwen3.8 已放开。未知模型原样透传。"""
    from ksadk.model_proxy.transform import clamp_reasoning_effort

    assert clamp_reasoning_effort("qwen3.7-max", "xhigh") == "high"
    assert clamp_reasoning_effort("qwen3.7-flash", "xhigh") == "high"
    assert clamp_reasoning_effort("qwen3.7-plus", "xhigh") == "high"
    assert clamp_reasoning_effort("qwen3.8-max", "xhigh") == "xhigh"
    assert clamp_reasoning_effort("glm-5.3", "xhigh") == "xhigh"
    assert clamp_reasoning_effort("deepseek-v4-pro", "xhigh") == "xhigh"
    assert clamp_reasoning_effort("qwen3.7-max", "high") == "high"
    assert clamp_reasoning_effort("qwen3.7-max", "medium") == "medium"
    assert clamp_reasoning_effort("qwen3.7-max", "weird") == "weird"

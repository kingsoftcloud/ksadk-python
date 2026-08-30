"""namespace 工具拍平 + 还原单测(假数据)。"""

from ksadk.model_proxy.namespace import (
    build_restore_map,
    flatten_namespace_tool_name,
    flatten_request_namespaces,
    restore_function_call,
)
from ksadk.model_proxy.transform import chat_to_response, responses_to_chat


def test_flatten_short_name_unchanged():
    assert flatten_namespace_tool_name("mcp__x", "foo") == "mcp__x__foo"


def test_flatten_long_name_truncated_with_hash():
    ns = "mcp__" + "a" * 60
    flat = flatten_namespace_tool_name(ns, "tool")
    assert len(flat) <= 64
    assert flat != f"{ns}__tool"  # 被截断了(原名超长)
    # 同输入确定性输出
    assert flatten_namespace_tool_name(ns, "tool") == flat


def test_build_restore_map_and_collision_first_wins():
    tools = [
        {
            "type": "namespace",
            "name": "mcp__fs",
            "tools": [
                {"type": "function", "name": "read"},
                {"type": "function", "name": "write"},
            ],
        },
        {"type": "function", "name": "shell"},  # 非 namespace 不进 map
    ]
    m = build_restore_map(tools)
    assert "mcp__fs__read" in m and m["mcp__fs__read"] == {"namespace": "mcp__fs", "name": "read"}
    assert "mcp__fs__write" in m
    assert "shell" not in m


def test_flatten_request_lifts_children_and_rewrites_input():
    body = {
        "model": "m",
        "input": [
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "go"}]},
            {
                "type": "function_call",
                "name": "read",
                "namespace": "mcp__fs",
                "call_id": "c1",
                "arguments": "{}",
            },
            {"type": "function_call_output", "call_id": "c1", "output": "ok"},
        ],
        "tools": [
            {
                "type": "namespace",
                "name": "mcp__fs",
                "tools": [
                    {
                        "type": "function",
                        "name": "read",
                        "description": "d",
                        "parameters": {"type": "object"},
                        "strict": True,
                    },
                ],
            },
        ],
        "tool_choice": {"type": "namespace", "name": "mcp__fs"},
    }
    restore = flatten_request_namespaces(body)
    # tools 提升为顶层 function(flat name),namespace 移除
    assert [t["name"] for t in body["tools"]] == ["mcp__fs__read"]
    assert body["tools"][0]["strict"] is True
    # input 的 namespace-qualified call 重写为 flat(去 namespace)
    fc = body["input"][1]
    assert fc["name"] == "mcp__fs__read" and "namespace" not in fc
    # tool_choice 的 namespace-typed 丢弃
    assert "tool_choice" not in body
    assert restore["mcp__fs__read"] == {"namespace": "mcp__fs", "name": "read"}


def test_flatten_collision_raises():
    body = {
        "tools": [
            {
                "type": "namespace",
                "name": "ns",
                "tools": [
                    {"type": "function", "name": "x"},
                    # 同 ns 同 name -> 同 flat,撞名
                    {"type": "function", "name": "x"},
                ],
            },
        ]
    }
    import pytest

    with pytest.raises(ValueError, match="撞名"):
        flatten_request_namespaces(body)


def test_responses_to_chat_then_restore_roundtrip():
    """端到端:namespace 工具拍平转 chat,响应 function_call 还原回 {name,namespace}。"""
    body = {
        "model": "m",
        "input": "call fs.read",
        "tools": [
            {
                "type": "namespace",
                "name": "mcp__fs",
                "tools": [
                    {
                        "type": "function",
                        "name": "read",
                        "description": "d",
                        "parameters": {"type": "object"},
                    },
                ],
            },
        ],
    }
    chat_req, restore_map = responses_to_chat(body)
    # chat tools 是嵌套 function,name=flat
    assert chat_req["tools"] == [
        {
            "type": "function",
            "function": {
                "name": "mcp__fs__read",
                "description": "d",
                "parameters": {"type": "object"},
            },
        }
    ]
    # chat 返回 function_call name=flat
    chat = {
        "id": "c1",
        "model": "m",
        "choices": [
            {
                "finish_reason": "tool_calls",
                "message": {
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "mcp__fs__read", "arguments": "{}"},
                        }
                    ],
                },
            }
        ],
        "usage": {},
    }
    resp = chat_to_response(chat, "r", restore_map)
    fc = next(o for o in resp["output"] if o["type"] == "function_call")
    # 还原:name=read, namespace=mcp__fs
    assert fc["name"] == "read" and fc["namespace"] == "mcp__fs"


def test_namespaced_custom_tool_roundtrip():
    """Codex namespace custom tools survive the Responses -> Chat roundtrip."""

    body = {
        "model": "m",
        "input": [
            {"type": "message", "role": "user", "content": "run it"},
            {
                "type": "custom_tool_call",
                "namespace": "functions",
                "name": "exec",
                "call_id": "call_previous",
                "input": "pwd",
            },
            {
                "type": "custom_tool_call_output",
                "call_id": "call_previous",
                "output": "ok",
            },
        ],
        "tools": [
            {
                "type": "namespace",
                "name": "functions",
                "tools": [
                    {
                        "type": "custom",
                        "name": "exec",
                        "description": "Execute JavaScript orchestration code",
                        "format": {
                            "type": "grammar",
                            "syntax": "lark",
                            "definition": "start: /.+/",
                        },
                    }
                ],
            }
        ],
    }

    chat_req, restore_map = responses_to_chat(body)

    assert chat_req["tools"] == [
        {
            "type": "function",
            "function": {
                "name": "functions__exec",
                "description": "Execute JavaScript orchestration code",
                "parameters": {
                    "type": "object",
                    "properties": {"input": {"type": "string"}},
                    "required": ["input"],
                },
            },
        }
    ]
    assert chat_req["messages"][1]["tool_calls"][0]["function"] == {
        "name": "functions__exec",
        "arguments": '{"input": "pwd"}',
    }
    assert restore_map["functions__exec"] == {
        "namespace": "functions",
        "name": "exec",
        "custom": "true",
    }

    chat = {
        "id": "c1",
        "model": "m",
        "choices": [
            {
                "finish_reason": "tool_calls",
                "message": {
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {
                                "name": "functions__exec",
                                "arguments": '{"input":"pwd"}',
                            },
                        }
                    ],
                },
            }
        ],
        "usage": {},
    }

    response = chat_to_response(chat, "r", restore_map)
    call = next(item for item in response["output"] if item["type"] == "custom_tool_call")
    assert call["namespace"] == "functions"
    assert call["name"] == "exec"
    assert call["input"] == "pwd"


def test_restore_function_call_no_map_passthrough():
    item = {"type": "function_call", "name": "shell", "call_id": "c"}
    assert restore_function_call(item, {}) is item  # 无映射原样


def test_restore_only_function_call_type():
    item = {"type": "message", "name": "mcp__fs__read"}
    restore = {"mcp__fs__read": {"namespace": "mcp__fs", "name": "read"}}
    restore_function_call(item, restore)
    assert item["name"] == "mcp__fs__read"  # 非 function_call 不动

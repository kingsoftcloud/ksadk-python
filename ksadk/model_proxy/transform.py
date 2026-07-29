"""OpenAI Responses API <-> Chat Completions API 纯转换器(无环境依赖).

映射逻辑参照 cc-switch transform_codex_chat.rs / streaming_codex_chat.rs /
codex_responses_sse.rs,用 Python 重写。本模块只做纯函数/纯状态机转换,
不读环境变量、不持有网络/配置 —— 配置由 config.py 注入,便于内化与测试。
"""

import json
import time


class UnsupportedToolsError(ValueError):
    """codex 请求携带了当前不支持转换的工具类型(namespace/MCP/web_search 等)。

    应 fail-fast 返回明确错误,而不是静默丢弃工具降级请求;根治靠 namespace 拍平。
    """


# ---------------------------------------------------------------------------
# 文本 / role / system 处理
# ---------------------------------------------------------------------------


def extract_text(content):
    """content: str | [{type,text}] -> str"""
    if isinstance(content, str):
        return content
    parts = []
    for p in content or []:
        if isinstance(p, dict) and p.get("text"):
            parts.append(p["text"])
    return "".join(parts)


def _chat_role(role):
    """responses role -> chat role(developer->system; latest_reminder/未知->user)。

    kimi-k3 等严格网关对 developer role 报 tokenization failed,必须映射。
    """
    if role in ("system", "developer"):
        return "system"
    if role == "assistant":
        return "assistant"
    if role == "tool":
        return "tool"
    return "user"


def collapse_system(msgs):
    """把所有 system 消息合并到头部一条(cc-switch collapse_system_messages_to_head)。"""
    chunks, rest = [], []
    for m in msgs:
        if m.get("role") == "system" and isinstance(m.get("content"), str) and m["content"].strip():
            chunks.append(m["content"])
        else:
            rest.append(m)
    return ([{"role": "system", "content": "\n\n".join(chunks)}] if chunks else []) + rest


# ---------------------------------------------------------------------------
# 请求: responses -> chat
# ---------------------------------------------------------------------------

# 除 messages/tools 外原样透传的字段(cc-switch EXTRA_CHAT_PASSTHROUGH_FIELDS 子集)
_PASSTHROUGH = (
    "temperature",
    "top_p",
    "top_logprobs",
    "logprobs",
    "stop",
    "seed",
    "frequency_penalty",
    "presence_penalty",
    "logit_bias",
    "response_format",
    "user",
    "metadata",
    "n",
    "service_tier",
)


def input_to_messages(inp):
    """responses 的 input(str | [items]) -> chat messages[]。

    处理 message / function_call / function_call_output;reasoning 历史不转发。
    """
    if inp is None:
        return []
    if isinstance(inp, str):
        return [{"role": "user", "content": inp}]
    if isinstance(inp, dict):
        inp = [inp]
    messages = []
    pending_tc = []

    def flush():
        if pending_tc:
            messages.append({"role": "assistant", "content": None, "tool_calls": list(pending_tc)})
            pending_tc.clear()

    for item in inp:
        if not isinstance(item, dict):
            continue
        t = item.get("type")
        if t in (None, "message"):
            flush()
            messages.append(
                {
                    "role": _chat_role(item.get("role", "user")),
                    "content": extract_text(item.get("content")),
                }
            )
        elif t == "function_call":
            pending_tc.append(
                {
                    "id": item.get("call_id") or f"call_{len(pending_tc)}",
                    "type": "function",
                    "function": {
                        "name": item.get("name"),
                        "arguments": item.get("arguments") or "",
                    },
                }
            )
        elif t == "function_call_output":
            flush()
            out = item.get("output")
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": item.get("call_id") or "",
                    "content": out if isinstance(out, str) else json.dumps(out, ensure_ascii=False),
                }
            )
        # reasoning / 其他类型: 跳过
    flush()
    return messages


def convert_tools(tools):
    """codex 扁平 function tool -> chat 嵌套 function tool,保留 strict。

    遇非 function 工具(namespace/MCP/web_search 等)fail-fast 抛 UnsupportedToolsError,
    不静默丢弃降级请求(codex 端禁用配置注入前的安全网)。
    """
    out = []
    unsupported = []
    for t in tools or []:
        if isinstance(t, dict) and t.get("type") == "function":
            fn = {
                "name": t.get("name"),
                "description": t.get("description", ""),
                "parameters": t.get("parameters", {"type": "object", "properties": {}}),
            }
            if "strict" in t:
                fn["strict"] = t["strict"]
            out.append({"type": "function", "function": fn})
        else:
            t = t or {}
            unsupported.append(t.get("type") or t.get("name") or "unknown")
    if unsupported:
        raise UnsupportedToolsError(
            f"不支持转换的 codex 工具类型: {unsupported};"
            "请在 codex 端禁用 multi_agent/web_search(v2 优先),或实现 namespace 拍平"
        )
    return out


def convert_tool_choice(tc):
    """responses tool_choice -> chat tool_choice。

    named 形式 {"type":"function","name":"x"} 必须嵌套为
    {"type":"function","function":{"name":"x"}}(chat 要求 function.name 嵌套)。
    """
    if tc is None or isinstance(tc, str):
        return tc  # auto / none / required
    if isinstance(tc, dict) and tc.get("type") == "function":
        name = tc.get("name") or (tc.get("function") or {}).get("name")
        return {"type": "function", "function": {"name": name}}
    return tc


def _convert_text_format(fmt):
    """responses text.format -> chat response_format(structured output 结构重组)。

    responses: {"type":"json_schema","name":...,"schema":...,"strict":...}
    chat:      {"type":"json_schema","json_schema":{"name":...,"schema":...,"strict":...}}
    """
    if not isinstance(fmt, dict):
        return None
    t = fmt.get("type")
    if t == "json_schema":
        js = {"name": fmt.get("name"), "schema": fmt.get("schema")}
        if "strict" in fmt:
            js["strict"] = fmt["strict"]
        return {"type": "json_schema", "json_schema": js}
    if t in ("json_object", "text"):
        return {"type": t}
    return None


def responses_to_chat(body):
    """responses 请求 -> chat 请求;返回 (chat_req, restore_map)。

    先拍平 namespace 工具(namespace.py),再用 convert_tools 转 chat 嵌套 function。
    restore_map 供响应侧把 flat function_call 名字还原回 {name, namespace}。
    """
    from .namespace import flatten_request_namespaces

    restore_map = flatten_request_namespaces(body)
    out = {"model": body.get("model")}
    msgs = []
    if body.get("instructions"):
        msgs.append({"role": "system", "content": extract_text(body["instructions"])})
    msgs += input_to_messages(body.get("input"))
    out["messages"] = collapse_system(msgs)
    if body.get("max_output_tokens") is not None:
        out["max_tokens"] = body["max_output_tokens"]
    for k in _PASSTHROUGH:
        if k in body:
            out[k] = body[k]
    # codex 特有字段(client.rs:862 固定发送):
    # reasoning effort / structured output(在 text.format,非顶层 response_format) / 显式缓存 key
    reasoning = body.get("reasoning")
    if isinstance(reasoning, dict) and reasoning.get("effort"):
        out["reasoning_effort"] = reasoning["effort"]
    text = body.get("text")
    if isinstance(text, dict):
        rf = _convert_text_format(text.get("format"))
        if rf is not None:
            out["response_format"] = rf
        if text.get("verbosity"):
            out["verbosity"] = text["verbosity"]
    if body.get("prompt_cache_key"):
        out["prompt_cache_key"] = body["prompt_cache_key"]
    tools = convert_tools(body.get("tools"))
    if tools:
        out["tools"] = tools
        if "tool_choice" in body:
            out["tool_choice"] = convert_tool_choice(body["tool_choice"])
        if "parallel_tool_calls" in body:
            out["parallel_tool_calls"] = body["parallel_tool_calls"]
    return out, restore_map


# ---------------------------------------------------------------------------
# 响应(非流式): chat -> responses
# ---------------------------------------------------------------------------


def convert_usage(u):
    """chat usage -> responses usage;透传前缀缓存命中数(否则 codex 看到 cached 恒 0)。"""
    u = u or {}
    ct = u.get("completion_tokens_details") or {}
    pt = u.get("prompt_tokens_details") or {}  # 平台有时返回 None
    return {
        "input_tokens": u.get("prompt_tokens", 0),
        "input_tokens_details": {"cached_tokens": pt.get("cached_tokens", 0)},
        "output_tokens": u.get("completion_tokens", 0),
        "total_tokens": u.get("total_tokens", 0),
        "output_tokens_details": {"reasoning_tokens": ct.get("reasoning_tokens", 0)},
    }


def _status_and_incomplete(finish_reason):
    """finish_reason -> (status, incomplete_reason)。

    length/content_filter 都不是 completed。
    """
    if finish_reason == "length":
        return "incomplete", "max_output_tokens"
    if finish_reason == "content_filter":
        return "incomplete", "content_filter"
    return "completed", None


def chat_to_response(chat, rid, restore_map=None):
    choice = (chat.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    output = []
    rc = msg.get("reasoning_content")
    if rc:
        output.append(
            {
                "id": f"rs_{rid}",
                "type": "reasoning",
                "summary": [{"type": "summary_text", "text": rc}],
            }
        )
    if msg.get("content"):
        output.append(
            {
                "id": f"{rid}_msg",
                "type": "message",
                "status": "completed",
                "role": "assistant",
                "content": [{"type": "output_text", "text": msg["content"], "annotations": []}],
            }
        )
    from .namespace import restore_function_call

    for i, tc in enumerate(msg.get("tool_calls") or []):
        fn = tc.get("function") or {}
        cid = tc.get("id") or f"call_{i}"
        item = restore_function_call(
            {
                "id": f"fc_{cid}",
                "type": "function_call",
                "status": "completed",
                "call_id": cid,
                "name": fn.get("name"),
                "arguments": fn.get("arguments") or "",
            },
            restore_map or {},
        )
        output.append(item)
    status, incomplete = _status_and_incomplete(choice.get("finish_reason"))
    resp = {
        "id": rid,
        "object": "response",
        "created_at": chat.get("created", int(time.time())),
        "status": status,
        "model": chat.get("model"),
        "output": output,
        "usage": convert_usage(chat.get("usage")),
    }
    if incomplete:
        resp["incomplete_details"] = {"reason": incomplete}
    return resp


# ---------------------------------------------------------------------------
# 流式: chat SSE -> responses SSE(每个 tool call 独立状态,修复并行截断)
# ---------------------------------------------------------------------------


class Streamer:
    """把 chat completions 流式 chunk 转成 responses SSE 事件。

    关键不变量:每个 tool call(按 index)独立维护 open/output_index/arguments,
    **index 切换时不互相关闭**,统一在 finalize 关闭 —— 修复并行 tool call 参数被截断。
    """

    def __init__(self, rid, model, restore_map=None):
        self.rid = rid
        self.model = model
        self.restore_map = restore_map or {}
        self.created = int(time.time())
        self._next_oi = 0
        self._reasoning = None  # {"oi","id","text"}
        self._message = None  # {"oi","id","text"}
        self._tool_calls = {}  # index -> {"oi","item_id","call_id","name","args"}
        self._completed = []  # [(oi, item)] 完成顺序
        self.usage = None
        self.finish = None

    # ---- SSE 信封(格式照 codex_responses_sse.rs) ----
    @staticmethod
    def ev(event, data):
        return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"

    def _resp(self, status, output=None, usage=None):
        r = {
            "id": self.rid,
            "object": "response",
            "created_at": self.created,
            "status": status,
            "model": self.model,
            "output": output or [],
        }
        if usage is not None:
            r["usage"] = usage
        return r

    def _alloc_oi(self):
        oi = self._next_oi
        self._next_oi += 1
        return oi

    def start(self):
        return [
            self.ev(
                "response.created",
                {"type": "response.created", "response": self._resp("in_progress")},
            ),
            self.ev(
                "response.in_progress",
                {"type": "response.in_progress", "response": self._resp("in_progress")},
            ),
        ]

    # ---- reasoning ----
    def _open_reasoning(self):
        oi = self._alloc_oi()
        rid = f"rs_{self.rid}"
        self._reasoning = {"oi": oi, "id": rid, "text": ""}
        return [
            self.ev(
                "response.output_item.added",
                {
                    "type": "response.output_item.added",
                    "output_index": oi,
                    "item": {
                        "id": rid,
                        "type": "reasoning",
                        "status": "in_progress",
                        "summary": [],
                    },
                },
            ),
            self.ev(
                "response.reasoning_summary_part.added",
                {
                    "type": "response.reasoning_summary_part.added",
                    "item_id": rid,
                    "output_index": oi,
                    "summary_index": 0,
                    "part": {"type": "summary_text", "text": ""},
                },
            ),
        ]

    def _close_reasoning(self):
        r = self._reasoning
        self._reasoning = None
        item = {
            "id": r["id"],
            "type": "reasoning",
            "summary": [{"type": "summary_text", "text": r["text"]}],
        }
        self._completed.append((r["oi"], item))
        return [
            self.ev(
                "response.reasoning_summary_text.done",
                {
                    "type": "response.reasoning_summary_text.done",
                    "item_id": r["id"],
                    "output_index": r["oi"],
                    "summary_index": 0,
                    "text": r["text"],
                },
            ),
            self.ev(
                "response.reasoning_summary_part.done",
                {
                    "type": "response.reasoning_summary_part.done",
                    "item_id": r["id"],
                    "output_index": r["oi"],
                    "summary_index": 0,
                    "part": {"type": "summary_text", "text": r["text"]},
                },
            ),
            self.ev(
                "response.output_item.done",
                {"type": "response.output_item.done", "output_index": r["oi"], "item": item},
            ),
        ]

    # ---- message ----
    def _open_message(self):
        oi = self._alloc_oi()
        mid = f"{self.rid}_msg"
        self._message = {"oi": oi, "id": mid, "text": ""}
        return [
            self.ev(
                "response.output_item.added",
                {
                    "type": "response.output_item.added",
                    "output_index": oi,
                    "item": {
                        "id": mid,
                        "type": "message",
                        "status": "in_progress",
                        "role": "assistant",
                        "content": [],
                    },
                },
            ),
            self.ev(
                "response.content_part.added",
                {
                    "type": "response.content_part.added",
                    "item_id": mid,
                    "output_index": oi,
                    "content_index": 0,
                    "part": {"type": "output_text", "text": "", "annotations": []},
                },
            ),
        ]

    def _close_message(self):
        m = self._message
        self._message = None
        item = {
            "id": m["id"],
            "type": "message",
            "status": "completed",
            "role": "assistant",
            "content": [{"type": "output_text", "text": m["text"], "annotations": []}],
        }
        self._completed.append((m["oi"], item))
        return [
            self.ev(
                "response.output_text.done",
                {
                    "type": "response.output_text.done",
                    "item_id": m["id"],
                    "output_index": m["oi"],
                    "content_index": 0,
                    "text": m["text"],
                },
            ),
            self.ev(
                "response.content_part.done",
                {
                    "type": "response.content_part.done",
                    "item_id": m["id"],
                    "output_index": m["oi"],
                    "content_index": 0,
                    "part": {"type": "output_text", "text": m["text"], "annotations": []},
                },
            ),
            self.ev(
                "response.output_item.done",
                {"type": "response.output_item.done", "output_index": m["oi"], "item": item},
            ),
        ]

    # ---- function_call(每个 index 独立) ----
    def _open_tool_call(self, idx, call_id, name):
        oi = self._alloc_oi()
        item_id = f"fc_{call_id}"
        self._tool_calls[idx] = {
            "oi": oi,
            "item_id": item_id,
            "call_id": call_id,
            "name": name,
            "args": "",
        }
        return [
            self.ev(
                "response.output_item.added",
                {
                    "type": "response.output_item.added",
                    "output_index": oi,
                    "item": {
                        "id": item_id,
                        "type": "function_call",
                        "status": "in_progress",
                        "call_id": call_id,
                        "name": name,
                        "arguments": "",
                    },
                },
            )
        ]

    def _close_tool_call(self, idx):
        from .namespace import restore_function_call

        tc = self._tool_calls.pop(idx)
        item = restore_function_call(
            {
                "id": tc["item_id"],
                "type": "function_call",
                "status": "completed",
                "call_id": tc["call_id"],
                "name": tc["name"],
                "arguments": tc["args"],
            },
            self.restore_map,
        )
        self._completed.append((tc["oi"], item))
        return [
            self.ev(
                "response.function_call_arguments.done",
                {
                    "type": "response.function_call_arguments.done",
                    "item_id": tc["item_id"],
                    "output_index": tc["oi"],
                    "arguments": tc["args"],
                },
            ),
            self.ev(
                "response.output_item.done",
                {"type": "response.output_item.done", "output_index": tc["oi"], "item": item},
            ),
        ]

    def handle(self, chunk):
        evts = []
        if chunk.get("usage"):
            self.usage = chunk["usage"]
        for choice in chunk.get("choices") or []:
            delta = choice.get("delta") or {}

            rt = delta.get("reasoning_content") or delta.get("reasoning")
            if rt:
                if self._reasoning is None:
                    evts += self._open_reasoning()
                self._reasoning["text"] += rt
                evts.append(
                    self.ev(
                        "response.reasoning_summary_text.delta",
                        {
                            "type": "response.reasoning_summary_text.delta",
                            "item_id": self._reasoning["id"],
                            "output_index": self._reasoning["oi"],
                            "summary_index": 0,
                            "delta": rt,
                        },
                    )
                )

            ct = delta.get("content")
            if ct:
                if self._reasoning is not None:
                    evts += self._close_reasoning()
                if self._message is None:
                    evts += self._open_message()
                self._message["text"] += ct
                evts.append(
                    self.ev(
                        "response.output_text.delta",
                        {
                            "type": "response.output_text.delta",
                            "item_id": self._message["id"],
                            "output_index": self._message["oi"],
                            "content_index": 0,
                            "delta": ct,
                        },
                    )
                )

            tcs = delta.get("tool_calls")
            if tcs:
                # 进入工具调用阶段:reasoning/message 与 tool call 不并行,先关掉
                if self._reasoning is not None:
                    evts += self._close_reasoning()
                if self._message is not None:
                    evts += self._close_message()
                for tc in tcs:
                    idx = tc.get("index", 0)
                    fn = tc.get("function") or {}
                    if idx not in self._tool_calls:
                        evts += self._open_tool_call(
                            idx, tc.get("id") or f"call_{idx}", fn.get("name") or ""
                        )
                    elif fn.get("name") and not self._tool_calls[idx]["name"]:
                        self._tool_calls[idx]["name"] = fn["name"]
                    if fn.get("arguments"):
                        self._tool_calls[idx]["args"] += fn["arguments"]
                        evts.append(
                            self.ev(
                                "response.function_call_arguments.delta",
                                {
                                    "type": "response.function_call_arguments.delta",
                                    "item_id": self._tool_calls[idx]["item_id"],
                                    "output_index": self._tool_calls[idx]["oi"],
                                    "delta": fn["arguments"],
                                },
                            )
                        )

            if choice.get("finish_reason"):
                self.finish = choice["finish_reason"]
        return evts

    def finalize(self):
        evts = []
        if self._reasoning is not None:
            evts += self._close_reasoning()
        if self._message is not None:
            evts += self._close_message()
        for idx in sorted(self._tool_calls):
            evts += self._close_tool_call(idx)
        items = [item for _, item in sorted(self._completed, key=lambda x: x[0])]
        if self.finish is None:
            # 断流:无合法 incomplete reason(官方仅 max_output_tokens/content_filter),发 failed
            resp = self._resp("failed", output=items, usage=convert_usage(self.usage))
            resp["error"] = {"message": "上游流中断(未收到 finish_reason)"}
            evts.append(self.ev("response.failed", {"type": "response.failed", "response": resp}))
            return evts
        status, incomplete = _status_and_incomplete(self.finish)
        resp = self._resp(status, output=items, usage=convert_usage(self.usage))
        if incomplete:
            resp["incomplete_details"] = {"reason": incomplete}
        # codex 仅在收到 response.incomplete 时才报错;incomplete 不能误发 completed
        event = "response.completed" if status == "completed" else "response.incomplete"
        evts.append(self.ev(event, {"type": event, "response": resp}))
        return evts

"""codex namespace 工具拍平 + 还原(responses↔chat 路径)。

codex 0.142+ 用 namespace 工具声明插件/MCP 工具:

    {"type":"namespace","name":"mcp__x__","tools":[{"type":"function","name":"foo",...}]}

并在 input 历史里发 namespace-qualified function_call:

    {"type":"function_call","name":"foo","namespace":"mcp__x__",...}

chat completions 只认顶层 ``function`` 工具(chat 嵌套)和 ``function.name``,
不认 ``namespace`` 字段。本模块把 namespace 工具**拍平**成顶层 function,
名字用确定性的 ``<namespace>__<child>``(超长 sha256 截断,与 cc-switch 一致),
响应侧再把 flat 名字**还原**回 ``{name, namespace}``,让 codex client 能匹配
自己的 namespace 工具注册表。

请求侧拍平 + 响应侧还原都从**同一份 request tools** 经
:func:`flatten_namespace_tool_name` 派生名字映射,前后一致,无需跨请求状态。

参照 cc-switch ``transform_codex_responses_namespace.rs``(用 Python 重写)。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

CHAT_TOOL_NAME_MAX_LEN = 64


def flatten_namespace_tool_name(namespace: str, name: str) -> str:
    """``<namespace>__<name>``;超长则尾部接 sha256 截断,保持在 64 字符内。

    截断按字符边界,保证返回值是合法 UTF-8(不会从多字节字符中间切断)。
    """
    full = f"{namespace}__{name}"
    if len(full) <= CHAT_TOOL_NAME_MAX_LEN:
        return full
    suffix = "__" + hashlib.sha256(full.encode("utf-8")).hexdigest()[:8]
    prefix_len = CHAT_TOOL_NAME_MAX_LEN - len(suffix)
    prefix = ""
    for ch in full:
        if len(prefix.encode("utf-8")) + len(ch.encode("utf-8")) > prefix_len:
            break
        prefix += ch
    return prefix + suffix


def _namespace_children(tool: dict) -> list[dict]:
    for key in ("tools", "children"):
        kids = tool.get(key)
        if isinstance(kids, list):
            return kids
    return []


def build_restore_map(tools: list[Any] | None) -> dict[str, dict[str, str]]:
    """从 request 的 namespace 工具声明建 ``flat -> {namespace, name}`` 映射。

    两个不同 child 拍平后撞名时,后者覆盖前者(与 cc-switch ``or_insert`` 不同,
    cc-switch 用 first-wins 并对撞名报错;这里取 first-wins + 调用方撞名检测)。
    """
    restore: dict[str, dict[str, str]] = {}
    if not tools:
        return restore
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        if tool.get("type") == "custom":
            # custom 工具（如 apply_patch freeform）拍平为普通 function 转发，
            # 响应侧据此把 function_call 还原成 custom_tool_call
            custom_name = (tool.get("name") or "").strip()
            if custom_name:
                restore.setdefault(
                    custom_name,
                    {"custom": "true", "namespace": "", "name": custom_name},
                )
            continue
        if tool.get("type") != "namespace":
            continue
        namespace = (tool.get("name") or "").strip()
        if not namespace:
            continue
        for child in _namespace_children(tool):
            if not isinstance(child, dict) or child.get("type") not in {
                "function",
                "custom",
            }:
                continue
            name = (child.get("name") or "").strip()
            if not name:
                continue
            flat = flatten_namespace_tool_name(namespace, name)
            entry = {"namespace": namespace, "name": name}
            if child.get("type") == "custom":
                entry["custom"] = "true"
            restore.setdefault(flat, entry)
    return restore


def _rewrite_qualified_calls(value: Any, owners: dict[str, dict[str, str]]) -> bool:
    """递归把 input 里的 namespace-qualified function_call 重写成 flat name。

    ``{type:function_call, name:child, namespace:ns}`` -> ``{type:function_call, name:flat}``
    (去掉 namespace 字段);只在 (ns, name) 与 owners 条目匹配时重写。
    """
    changed = False
    if isinstance(value, list):
        for item in value:
            changed |= _rewrite_qualified_calls(item, owners)
    elif isinstance(value, dict):
        if value.get("type") in {"function_call", "custom_tool_call"}:
            namespace = (value.get("namespace") or "").strip()
            name = (value.get("name") or "").strip()
            if namespace and name:
                flat = flatten_namespace_tool_name(namespace, name)
                entry = owners.get(flat)
                if entry and entry["namespace"] == namespace and entry["name"] == name:
                    value["name"] = flat
                    value.pop("namespace", None)
                    changed = True
        for child in value.values():
            changed |= _rewrite_qualified_calls(child, owners)
    return changed


def flatten_request_namespaces(body: dict) -> dict[str, dict[str, str]]:
    """原地拍平 body 的 namespace 工具,返回 restore_map(供响应侧还原)。

    - tools:namespace children 提升为顶层 responses function 工具(flat name),
      原 namespace 工具移除;检测撞名(两个 child 拍平后同名)则 raise。
    - input:namespace-qualified function_call 重写为 flat name。
    - tool_choice:namespace-typed 丢弃(chat 不认)。

    返回 ``flat -> {namespace, name}`` 映射;无 namespace 工具时返回空 dict,
    调用方可据此跳过响应侧还原。
    """
    tools = body.get("tools")
    if not isinstance(tools, list):
        return {}
    restore = build_restore_map(tools)
    flat_tools: list[dict] = []
    seen_flat: set[str] = set()
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        if tool.get("type") == "namespace":
            namespace = (tool.get("name") or "").strip()
            for child in _namespace_children(tool):
                if not isinstance(child, dict) or child.get("type") not in {
                    "function",
                    "custom",
                }:
                    continue
                name = (child.get("name") or "").strip()
                if not name or not namespace:
                    continue
                flat = flatten_namespace_tool_name(namespace, name)
                if flat in seen_flat:
                    raise ValueError(f"namespace 拍平撞名:{flat} 来自不同 child,上游无法消歧")
                seen_flat.add(flat)
                if child.get("type") == "custom":
                    flat_tools.append(
                        {
                            "type": "custom",
                            "name": flat,
                            "description": child.get("description", ""),
                            **({"format": child["format"]} if "format" in child else {}),
                        }
                    )
                else:
                    flat_tools.append(
                        {
                            "type": "function",
                            "name": flat,
                            "description": child.get("description", ""),
                            "parameters": child.get(
                                "parameters", {"type": "object", "properties": {}}
                            ),
                            **({"strict": child["strict"]} if "strict" in child else {}),
                        }
                    )
        else:
            flat_tools.append(tool)
    body["tools"] = flat_tools
    if restore and isinstance(body.get("input"), list):
        _rewrite_qualified_calls(body["input"], restore)
    tc = body.get("tool_choice")
    if isinstance(tc, dict) and tc.get("type") == "namespace":
        body.pop("tool_choice", None)
    return restore


def restore_function_call(item: dict, restore_map: dict[str, dict[str, str]]) -> dict:
    """把响应里 function_call 的 flat name 还原成 ``{name, namespace}``(原地改)。

    无映射或 name 不在映射中时原样返回(普通 function 工具的 call 不受影响)。
    """
    if not restore_map or item.get("type") != "function_call":
        return item
    flat = item.get("name")
    entry = restore_map.get(flat) if isinstance(flat, str) else None
    if entry:
        if entry.get("custom") == "true":
            # function_call(JSON arguments) -> custom_tool_call(原始文本 input)
            raw_args = item.pop("arguments", "") or ""
            try:
                payload = json.loads(raw_args)
                text_input = (
                    payload.get("input", raw_args) if isinstance(payload, dict) else raw_args
                )
            except Exception:
                text_input = raw_args
            item["type"] = "custom_tool_call"
            item["name"] = entry["name"]
            item["input"] = text_input
            if entry.get("namespace"):
                item["namespace"] = entry["namespace"]
            else:
                item.pop("namespace", None)
            return item
        item["name"] = entry["name"]
        item["namespace"] = entry["namespace"]
    return item

"""Lower canonical Chat messages without losing tools, media, or their order.

Host-local request conversion only: no file reads, URL fetches, new authority,
reasoning reconstruction, or repairs of malformed historical tool input.
"""

from __future__ import annotations

import base64
import binascii
import copy
import json
from collections.abc import Iterator
from typing import Any, NoReturn
from urllib.parse import urlsplit

from ksadk._model_anthropic import anthropic_content, anthropic_projection
from ksadk._model_json import load_model_json
from ksadk.studio.errors import StudioError

MODEL_REQUEST_PROJECTION_POLICY = "canonical-chat-provider-projection/v2"

_IMAGE_HEADERS = {f"data:image/{kind};base64" for kind in ("png", "jpeg", "webp", "gif")}


def _invalid() -> NoReturn:
    raise StudioError(
        "MODEL_REQUEST_INVALID", "模型请求包含不支持或不完整的消息结构", status_code=400,
    ) from None


def _identity(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        _invalid()
    return value


def _content(value: object) -> str | list[dict[str, Any]]:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if not isinstance(value, list):
        _invalid()
    blocks = []
    for raw in value:
        if not isinstance(raw, dict):
            _invalid()
        if raw.get("type") == "text" and isinstance(raw.get("text"), str):
            blocks.append({"type": "text", "text": raw["text"]})
        elif raw.get("type") == "image_url" and isinstance(raw.get("image_url"), dict):
            source = raw["image_url"]
            url = _identity(source.get("url"))
            if url.startswith("data:"):
                header, separator, encoded = url.partition(",")
                if header not in _IMAGE_HEADERS or not separator or not encoded:
                    _invalid()
                try:
                    base64.b64decode(encoded, validate=True)
                except (ValueError, binascii.Error):
                    _invalid()
            else:
                try:
                    parsed = urlsplit(url)
                except ValueError:
                    _invalid()
                if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                    _invalid()
            detail = source.get("detail")
            if detail is not None and (
                not isinstance(detail, str) or detail not in {"auto", "low", "high"}
            ):
                _invalid()
            blocks.append({"type": "image_url", "image_url": {
                "url": url, **({"detail": detail} if detail is not None else {}),
            }})
        else:
            _invalid()
    return blocks


def _calls(value: object) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list):
        _invalid()
    calls = []
    for raw in value:
        if not isinstance(raw, dict) or raw.get("type", "function") != "function":
            _invalid()
        function = raw.get("function", raw)
        if not isinstance(function, dict):
            _invalid()
        identity, name = _identity(raw.get("id")), _identity(function.get("name"))
        arguments = function.get("arguments")
        try:
            encoded = arguments if isinstance(arguments, str) else json.dumps(
                arguments, ensure_ascii=False, allow_nan=False, separators=(",", ":"),
            )
            decoded = load_model_json(encoded)
        except (ValueError, TypeError, OverflowError, UnicodeError, RecursionError):
            _invalid()
        if not isinstance(decoded, dict):
            _invalid()
        calls.append({"id": identity, "name": name, "arguments": encoded, "input": decoded})
    if len({call["id"] for call in calls}) != len(calls):
        _invalid()
    return calls


def _messages(messages: object) -> Iterator[dict[str, Any]]:
    if not isinstance(messages, list):
        _invalid()
    pending: set[str] = set()
    for raw in messages:
        if not isinstance(raw, dict):
            _invalid()
        role = raw.get("role")
        if not isinstance(role, str) or role not in {
            "system", "developer", "user", "assistant", "tool",
        }:
            _invalid()
        if role != "tool" and pending:
            _invalid()
        calls = _calls(raw.get("tool_calls"))
        if calls and role != "assistant":
            _invalid()
        content = _content(raw.get("content"))
        if role in {"system", "developer", "assistant"} and isinstance(content, list):
            if any(block["type"] != "text" for block in content):
                _invalid()
        item = {"role": role, "content": content, "calls": calls}
        if "anthropic_content" in raw:
            if role != "assistant":
                _invalid()
            try:
                original = anthropic_content(raw["anthropic_content"])
                original_text, original_calls = anthropic_projection(
                    original, raw.get("anthropic_tool_id_map", ()),
                )
            except (ValueError, TypeError):
                _invalid()
            expected = [{"call_id": c["id"], "name": c["name"], "arguments": c["input"]}
                        for c in calls]
            if original_text != _system_text(content) or original_calls != expected:
                _invalid()
            item["anthropic_content"] = original
        if role == "assistant":
            pending = {call["id"] for call in calls}
        elif role == "tool":
            identity = _identity(raw.get("tool_call_id"))
            if identity not in pending:
                _invalid()
            pending.remove(identity)
            item["tool_call_id"] = identity
            if "is_error" in raw:
                if type(raw["is_error"]) is not bool:
                    _invalid()
                item["is_error"] = raw["is_error"]
        yield item
    if pending:
        _invalid()


def _parts(content: str | list[dict], *, wire: str, assistant: bool = False) -> list[dict]:
    blocks = [{"type": "text", "text": content}] if isinstance(content, str) else content
    projected = []
    for block in blocks:
        if block["type"] == "text":
            kind = ("output_text" if assistant else "input_text") if wire == "responses" else "text"
            projected.append({"type": kind, "text": block["text"]})
            continue
        image = block["image_url"]
        if wire == "responses":
            projected.append({"type": "input_image", "image_url": image["url"],
                              **({"detail": image["detail"]} if "detail" in image else {})})
        else:
            url = image["url"]
            if url.startswith("data:"):
                header, _, encoded = url.partition(",")
                source = {"type": "base64", "media_type": header[5:].split(";")[0], "data": encoded}
            else:
                source = {"type": "url", "url": url}
            projected.append({"type": "image", "source": source})
    return projected


def _system_text(content: str | list[dict]) -> str:
    return content if isinstance(content, str) else "\n".join(block["text"] for block in content)


def responses_messages(messages: list[dict]) -> tuple[list[str], list[dict]]:
    instructions, items = [], []
    for message in _messages(messages):
        if "anthropic_content" in message:
            _invalid()
        role, content = message["role"], message["content"]
        if role in {"system", "developer"}:
            instructions.append(_system_text(content))
        elif role == "tool":
            items.append({"type": "function_call_output", "call_id": message["tool_call_id"],
                          "output": content if isinstance(content, str)
                          else _parts(content, wire="responses")})
        else:
            if content or not message["calls"]:
                items.append({"role": role, "content": _parts(
                    content, wire="responses", assistant=role == "assistant",
                )})
            items.extend({"type": "function_call", "call_id": call["id"], "name": call["name"],
                          "arguments": call["arguments"]} for call in message["calls"])
    return instructions, items


def anthropic_messages(
    messages: list[dict], *, mid_system: bool = False,
) -> tuple[list[str], list[dict]]:
    """Keep declared operator messages at their original conversation boundary.

    Only plain content is projected: no beta lifetime, effort, or tool changes.
    Unknown/opted-out endpoint profiles retain the legacy top-level projection.
    """
    if type(mid_system) is not bool:
        _invalid()
    system, items = [], []
    wire_ids = {}
    for message in _messages(messages):
        role, content = message["role"], message["content"]
        if role in {"system", "developer"}:
            if not mid_system or not items:
                system.append(_system_text(content))
            else:
                items.append({"role": "system", "content": content})
            continue
        if role == "tool":
            item = {"role": "user", "content": [{
                "type": "tool_result", "tool_use_id": wire_ids.get(
                    message["tool_call_id"], message["tool_call_id"],
                ),
                "content": (content if isinstance(content, str)
                            else _parts(content, wire="anthropic")),
                **({"is_error": message["is_error"]} if "is_error" in message else {}),
            }]}
        else:
            if "anthropic_content" in message:
                _, original_calls = anthropic_projection(message["anthropic_content"])
                wire_ids = {host["id"]: original["call_id"]
                            for host, original in zip(message["calls"], original_calls)}
            else:
                wire_ids = {}
            blocks = (copy.deepcopy(message["anthropic_content"])
                      if "anthropic_content" in message else
                      content if isinstance(content, str) else _parts(content, wire="anthropic"))
            if message["calls"] and "anthropic_content" not in message:
                blocks = _parts(content, wire="anthropic") if content else []
                blocks.extend({"type": "tool_use", "id": call["id"], "name": call["name"],
                               "input": call["input"]} for call in message["calls"])
            item = {"role": role, "content": blocks}
        previous = items[-1] if items else None
        if previous is not None and previous["role"] == item["role"]:
            first, second = previous["content"], item["content"]
            if isinstance(first, str) and isinstance(second, str):
                previous["content"] = first + "\n\n" + second if first else second
            else:
                previous["content"] = (
                    _parts(first, wire="anthropic") if isinstance(first, str) else first
                ) + (_parts(second, wire="anthropic") if isinstance(second, str) else second)
        else:
            items.append(item)
    if mid_system:
        # Consecutive operator messages form one section. They must follow a
        # user (including tool results) and precede an assistant or end-of-input.
        # _messages already forbids inserting them in an unfinished tool batch.
        index = 0
        while index < len(items):
            if items[index]["role"] != "system":
                index += 1
                continue
            if index == 0 or items[index - 1]["role"] != "user":
                _invalid()
            while index < len(items) and items[index]["role"] == "system":
                index += 1
            if index < len(items) and items[index]["role"] != "assistant":
                _invalid()
    return system, items

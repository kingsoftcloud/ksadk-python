"""Pure original-content decoding for client-executed Anthropic turns."""

import copy


def anthropic_content(value: object) -> list[dict]:
    if not isinstance(value, list):
        raise ValueError("Anthropic content must be an array")
    for block in value:
        if not isinstance(block, dict):
            raise ValueError("Anthropic content requires typed blocks")
        kind = block.get("type")
        fields = {
            "text": ("text",), "thinking": ("thinking", "signature"),
            "redacted_thinking": ("data",), "tool_use": ("id", "name"),
        }.get(kind)
        if fields is None:
            raise ValueError("unsupported Anthropic replay block")
        if any(not isinstance(block.get(name), str) for name in fields):
            raise ValueError("invalid Anthropic replay block field")
        if kind == "tool_use" and (
            not block["id"].strip() or not block["name"].strip()
            or not isinstance(block.get("input"), dict)
        ):
            raise ValueError("invalid Anthropic tool block")
    return copy.deepcopy(value)


def anthropic_projection(content: list[dict], id_map: object = ()) -> tuple[str, list[dict]]:
    """Separate visible/call facts while retaining the original replay elsewhere."""
    blocks = anthropic_content(content)
    text = "".join(block["text"] for block in blocks if block["type"] == "text")
    calls = [{"call_id": block["id"], "name": block["name"], "arguments": block["input"]}
             for block in blocks if block["type"] == "tool_use"]
    originals = {call["call_id"] for call in calls}
    if len(originals) != len(calls) or not isinstance(id_map, (tuple, list)):
        raise ValueError("invalid original Anthropic tool identities")
    mapped = {}
    for pair in id_map:
        if (not isinstance(pair, (tuple, list)) or len(pair) != 2
                or any(not isinstance(value, str) or not value.strip() for value in pair)
                or pair[0] not in originals or pair[0] in mapped):
            raise ValueError("invalid Anthropic host identity projection")
        mapped[pair[0]] = pair[1]
    for call in calls:
        call["call_id"] = mapped.get(call["call_id"], call["call_id"])
    if len({call["call_id"] for call in calls}) != len(calls):
        raise ValueError("colliding Anthropic host identities")
    return text, calls

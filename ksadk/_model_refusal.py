"""Typed provider refusal signals, independent of transport and retry policy."""


def chat_refusal(value: object) -> bool:
    if value is None:
        return False
    if type(value) is not str:
        raise ValueError("invalid model refusal field")
    return bool(value)


def anthropic_refusal(details: object) -> bool:
    if details is None:
        return False
    if not isinstance(details, dict) or details.get("type") != "refusal":
        raise ValueError("unsupported model stop details")
    for field in ("category", "explanation"):
        if details.get(field) is not None and type(details[field]) is not str:
            raise ValueError("invalid model stop details field")
    return True

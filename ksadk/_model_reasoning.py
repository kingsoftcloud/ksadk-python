"""Pure Chat reasoning decoding; private protocol never becomes public text."""


def chat_reasoning(message: dict) -> str:
    """Normalize the supported alias without hiding invalid or conflicting data.

    A missing/null field and an empty string both carry no text. Providers may
    include an empty native field alongside the alias; two nonempty fields
    must agree. This is decoding, independent of whether a host retains replay.
    """
    native, alias = message.get("reasoning_content"), message.get("reasoning")
    for value in (native, alias):
        if value is not None and not isinstance(value, str):
            raise ValueError("invalid private reasoning field")
    if native and alias and native != alias:
        raise ValueError("conflicting private reasoning fields")
    return native or alias or ""

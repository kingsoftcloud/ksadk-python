"""Compile explicit object field lists; never infer field values or semantics.

Only the recognized final-output clauses supplied by Studio are eligible.
Bare keys are ASCII identifiers; other supported keys need explicit quotes.
Unknown syntax, duplicate keys and conflicting declarations add no constraint.
Nested item declarations are not used to infer an array or field value type.
"""

import re

_KEY = r"[A-Za-z_][A-Za-z0-9_.-]{0,63}"
_QUOTED_KEY = rf"(?:{_KEY}|[\u4e00-\u9fff]{{1,32}})"
_TOKEN = rf'(?:{_KEY}|`{_QUOTED_KEY}`|"{_QUOTED_KEY}")'
_SEPARATOR = r"\s*(?:[,，、]|\s+(?:and|和|与)\s+)\s*"
_LIST = re.compile(rf"{_TOKEN}(?:{_SEPARATOR}{_TOKEN})*", re.I)
_KEY_CLAUSE = re.compile(
    r"\s*(?:(?:最后|最终)(?:请)?|请)?(?:只|仅)(?:回复|输出|返回)\s*"
    r"(?:一个|上述|该)?\s*JSON\s*对象\s*[,，]\s*(?:键|字段)(?:仅)?(?:为|是)\s*(?P<keys>.*)",
    re.I,
)
_ITEM_CLAUSE = re.compile(
    rf"\s*(?P<field>{_TOKEN})\s*的?\s*每项(?:键|字段)(?:为|是)\s*(?P<keys>.*)"
)


def _keys(listing):
    listing = listing.strip()
    if not _LIST.fullmatch(listing):
        return None
    tokens = re.split(_SEPARATOR, listing, flags=re.I)
    keys = tuple(t.strip('`"') for t in tokens)
    if len(keys) > 32 or len(set(keys)) != len(keys):
        return None
    return keys


def declared_object_fields(clauses):
    sets = []
    for shape, clause in clauses:
        if shape != "json_object":
            return None
        parts = re.split(r"[;；]", clause)
        match = _KEY_CLAUSE.fullmatch(parts[0])
        if not match:
            continue
        keys = _keys(match["keys"])
        if keys is None:
            return None
        # Extra output modifiers cannot be silently discarded. Recognize a
        # bounded item declaration without inferring its collection/value type.
        for suffix in parts[1:]:
            item = _ITEM_CLAUSE.fullmatch(suffix)
            if not item or item["field"].strip('`"') not in keys or _keys(item["keys"]) is None:
                return None
        sets.append(frozenset(keys))
    if not sets or len(set(sets)) != 1:
        return None
    return tuple(sorted(sets[0]))

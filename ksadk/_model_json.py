"""Pure model JSON decoding without duplicate keys or non-finite numbers.

Callers retain ownership of protocol classification and tool error receipts.
This decoder never repairs input, coerces containers or records model text.
"""

import json
import math


class ModelJSONPolicyError(ValueError):
    """Parsed JSON cannot identify one finite value; never reinterpret as text."""


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ModelJSONPolicyError("duplicate model JSON key")
        result[key] = value
    return result


def _reject_constant(_value: str) -> object:
    raise ModelJSONPolicyError("non-finite model JSON number")


def _finite_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ModelJSONPolicyError("model JSON number overflow")
    return number


def load_model_json(value: str | bytes) -> object:
    try:
        return json.loads(value, object_pairs_hook=_unique_object,
                          parse_constant=_reject_constant, parse_float=_finite_float)
    except RecursionError:
        raise ModelJSONPolicyError("model JSON nesting exceeds decoder capacity") from None

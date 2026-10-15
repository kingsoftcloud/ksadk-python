"""Pure validation of pinned tool input; never normalize or grant permission."""

from __future__ import annotations

import json

from jsonschema import FormatChecker
from jsonschema.exceptions import ValidationError as SchemaValidationError
from jsonschema.validators import validator_for
from pydantic import JsonValue

from .tool_argument_feedback import argument_error


def _reject_external_refs(value: object) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if key in {"$ref", "$dynamicRef", "$recursiveRef"}:
                if not isinstance(child, str) or not child.startswith("#"):
                    raise ValueError("tool schemas may only reference local fragments")
            _reject_external_refs(child)
    elif isinstance(value, list):
        for child in value:
            _reject_external_refs(child)


class ToolInputValidator:
    """The same detached schema and diagnostic semantics at both boundaries.

    The Kernel uses this before entering a ToolPort. StrictToolExecutor keeps
    a compiled instance alongside its pinned callback and checks again before
    current authorization. Success is only a schema fact, never approval.
    """

    def __init__(self, parameters: dict[str, JsonValue]) -> None:
        try:
            self._parameters = json.loads(json.dumps(parameters, allow_nan=False))
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid tool schema") from exc
        _reject_external_refs(self._parameters)
        try:
            validator_class = validator_for(self._parameters)
            validator_class.check_schema(self._parameters)
            self._validator = validator_class(self._parameters, format_checker=FormatChecker())
        except Exception as exc:
            raise ValueError("invalid tool schema") from exc

    def rejection(self, arguments: dict[str, JsonValue]) -> dict[str, JsonValue] | None:
        try:
            # Validate detached finite JSON without replacing the admitted
            # arguments. Defaults and coercion never rewrite an action.
            value = json.loads(json.dumps(arguments, allow_nan=False))
            self._validator.validate(value)
        except SchemaValidationError as exc:
            return argument_error(exc, self._parameters, self._validator)
        except Exception:
            return {"code": "invalid_arguments"}
        return None

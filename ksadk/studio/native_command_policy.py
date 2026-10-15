"""Declare command timeouts before admission; never rewrite explicit values."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy

from jsonschema import FormatChecker
from jsonschema.exceptions import ValidationError
from jsonschema.validators import validator_for


class StudioCommandTimeoutPolicy:
    """A locked SDK command policy, separate from backend resource ceilings."""

    def __init__(self, parameters: dict, *, default: int, maximum: int) -> None:
        constraint = {"type": "integer", "minimum": 1, "maximum": maximum}
        self.parameters = deepcopy(parameters)
        properties = self.parameters.get("properties")
        if isinstance(properties, dict) and "timeout" in properties:
            # Retain every original constraint, including enum, nullable unions
            # and local references. Defaults are declared intrinsic semantics;
            # JSON Schema validation itself does not insert them.
            properties["timeout"] = {
                **constraint, "default": default,
                "allOf": [properties["timeout"]],
            }
        else:
            self.parameters.setdefault("allOf", []).append({
                "type": "object", "properties": {"timeout": constraint},
            })
        validator_class = validator_for(self.parameters)
        validator_class.check_schema(self.parameters)
        self._validator = validator_class(self.parameters, format_checker=FormatChecker())
        self.default = default
        self.digest = "sha256:" + hashlib.sha256(json.dumps({
            "version": "studio.command-timeout-admission/v1",
            "default": default, "maximum": maximum,
        }, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    def prepare(self, arguments: dict) -> dict | None:
        prepared = dict(arguments)
        # Only omission uses the declared default. Explicit null, zero or an
        # out-of-range value cannot silently acquire different semantics.
        prepared.setdefault("timeout", self.default)
        try:
            self._validator.validate(prepared)
        except ValidationError:
            # A restricted original schema may not accept the default. The
            # failure is definite and precedes any execution or quota reserve.
            return None
        return prepared

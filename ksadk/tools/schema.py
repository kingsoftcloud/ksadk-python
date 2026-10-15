"""Model-facing contracts for trusted SDK callables, independent of runtimes."""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Any, get_type_hints

from pydantic import ConfigDict, create_model


def callable_parameters(function: Callable[..., Any]) -> dict[str, Any]:
    """Derive a closed JSON argument schema from a trusted Python declaration.

    Only SDK functions may use this helper. Uploaded Python tools keep their
    AST inspection boundary; resolving their annotations could execute code.
    Runtime adapters may decorate functions without owning this contract.
    """
    annotations = get_type_hints(function, include_extras=True)
    fields: dict[str, Any] = {}
    for name, parameter in inspect.signature(function).parameters.items():
        if parameter.kind not in {
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.KEYWORD_ONLY,
        }:
            raise ValueError("SDK tool parameters must have explicit keyword names")
        fields[name] = (
            annotations.get(name, Any),
            ... if parameter.default is inspect.Parameter.empty else parameter.default,
        )
    model = create_model(
        function.__name__, __config__=ConfigDict(extra="forbid"), **fields
    )
    return model.model_json_schema()

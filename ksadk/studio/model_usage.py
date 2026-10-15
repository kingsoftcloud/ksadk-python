"""Project provider usage without inventing or coercing reported counters."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal

from ksadk.studio.contracts import Usage
from ksadk.studio.errors import StudioError

Wire = Literal["chat", "responses", "anthropic"]


def _invalid() -> StudioError:
    # A protocol error is not a transient transport error. No provider values
    # or exception body belong in the public failure or a retry diagnostic.
    return StudioError("MODEL_RESPONSE_INVALID", "Invalid model usage report", status_code=422)


def _counter(value: object) -> int:
    if type(value) is not int or value < 0:
        raise _invalid()
    return value


def _fields(value: object, wire_api: Wire) -> dict[str, int]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise _invalid()
    input_key, output_key = (
        ("prompt_tokens", "completion_tokens") if wire_api == "chat"
        else ("input_tokens", "output_tokens")
    )
    names = [input_key, output_key]
    if wire_api == "anthropic":
        names.extend(("cache_read_input_tokens", "cache_creation_input_tokens"))
    else:
        names.append("total_tokens")
    counters = {
        name: _counter(value[name]) for name in names
        if name in value and not (
            wire_api == "anthropic" and name.startswith("cache_") and value[name] is None
        )
    }
    if wire_api != "anthropic":
        details = (
            ("prompt_tokens_details", "completion_tokens_details") if wire_api == "chat"
            else ("input_tokens_details", "output_tokens_details")
        )
        for envelope, name in zip(details, ("cached_tokens", "reasoning_tokens"), strict=True):
            subdivision = value.get(envelope)
            if subdivision is None:
                continue
            if not isinstance(subdivision, Mapping):
                raise _invalid()
            if name in subdivision:
                counters[name] = _counter(subdivision[name])
    return counters


def _report(counters: dict[str, int], wire_api: Wire) -> Usage | None:
    input_key, output_key = (
        ("prompt_tokens", "completion_tokens") if wire_api == "chat"
        else ("input_tokens", "output_tokens")
    )
    if input_key not in counters or output_key not in counters:
        # The Studio DTO has integer placeholders for unknown usage. A partial
        # provider envelope must never assert their defaults were reported.
        return None
    input_tokens, output_tokens = counters[input_key], counters[output_key]
    if wire_api == "anthropic":
        # Messages reports non-cached input separately from cache reads/writes.
        # Canonical input is inclusive; only cache reads map to cached_input.
        cached = counters.get("cache_read_input_tokens", 0)
        input_tokens += cached + counters.get("cache_creation_input_tokens", 0)
        total_tokens = input_tokens + output_tokens
    else:
        cached = counters.get("cached_tokens", 0)
        total_tokens = counters.get("total_tokens", input_tokens + output_tokens)
    return Usage(
        input_tokens=input_tokens, output_tokens=output_tokens, total_tokens=total_tokens,
        cached_input_tokens=cached, reasoning_output_tokens=counters.get("reasoning_tokens", 0),
        reported=True, source="model-provider",
    )


def provider_usage(value: object, *, wire_api: Wire) -> Usage | None:
    """Unknown/partial stays absent; malformed supplied fields are rejected.

    A complete report needs both genuine integer primary counters, including
    an explicit pair of zeroes. Optional details follow each wire's meaning.
    """
    return _report(_fields(value, wire_api), wire_api)


@dataclass
class AnthropicUsageAccumulator:
    """Request-local, right-biased merge of Messages' split usage reports."""

    _counters: dict[str, int] = field(default_factory=dict)

    def update(self, value: object) -> Usage | None:
        # Missing fields leave known values intact; zero is a defined update.
        # Validate before mutation, so a rejected update cannot erase evidence.
        self._counters.update(_fields(value, "anthropic"))
        return self.report()

    def report(self) -> Usage | None:
        return _report(self._counters, "anthropic")

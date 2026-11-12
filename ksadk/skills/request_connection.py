"""Transient Skill Service connection at the conversation request boundary.

Only selectors enter the conversation pipeline. Connection values live in a
ContextVar, inherited by execution tasks and thread offloads, never graph config.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import re
from collections.abc import Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import wraps
from types import MappingProxyType
from typing import Any

CONNECTION_FIELDS = frozenset(
    {
        "service_url",
        "account_id",
        "region",
        "service_token",
        "access_key",
        "secret_key",
        "security_token",
        "custom_source",
    }
)


@dataclass(frozen=True)
class RequestSkillConnection:
    # Keep even invalid values transient so the Agent can reject them safely.
    values: Mapping[str, Any] = field(repr=False)

    def __post_init__(self):
        object.__setattr__(self, "values", MappingProxyType(dict(self.values)))

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(
            "\0".join(
                str(self.values.get(key) or "").strip() for key in sorted(CONNECTION_FIELDS)
            ).encode()
        ).hexdigest()


_connection: ContextVar[RequestSkillConnection | None] = ContextVar(
    "ksadk_request_skill_connection", default=None
)


def current_skill_connection() -> RequestSkillConnection | None:
    return _connection.get()


def _sanitize_metadata(metadata: Any) -> tuple[Any, list[RequestSkillConnection]]:
    if not isinstance(metadata, Mapping):
        return metadata, []
    result = dict(metadata)
    connections = []
    raw = result.get("skill_source")
    if isinstance(raw, Mapping):
        if {"ak", "sk", "token", "credentials"}.intersection(raw):
            raise ValueError("Unsupported Skill connection credential fields")
        for key in ("service_token", "access_key", "secret_key", "security_token"):
            if raw.get(key) is not None and not isinstance(raw[key], str):
                raise ValueError("Skill connection credentials must be strings")
        access_key = (raw.get("access_key") or "").strip()
        secret_key = (raw.get("secret_key") or "").strip()
        if bool(access_key) != bool(secret_key):
            raise ValueError("Both signing credentials are required")
        if (raw.get("service_token") or "").strip() and (access_key or secret_key):
            raise ValueError("Skill connections cannot mix Bearer and signing credentials")
        if (raw.get("security_token") or "").strip() and not (access_key and secret_key):
            raise ValueError("Security token requires signing credentials")
        if CONNECTION_FIELDS.intersection(raw):
            connections.append(
                RequestSkillConnection({key: raw[key] for key in CONNECTION_FIELDS if key in raw})
            )
        result["skill_source"] = {
            key: value for key, value in raw.items() if key not in CONNECTION_FIELDS
        }
    if isinstance(result.get("agentengine"), Mapping):
        result["agentengine"], nested = _sanitize_metadata(result["agentengine"])
        connections.extend(nested)
    return result, connections


@contextmanager
def skill_connection_scope(request_metadata=None, model_metadata=None):
    """Sanitize before preparation/persistence and restore on every exit path."""
    request, request_connections = _sanitize_metadata(request_metadata)
    model, model_connections = _sanitize_metadata(model_metadata)
    connections = request_connections + model_connections
    # Conflicting channels remain selector-visible and are rejected by the Agent.
    connection = connections[0] if connections else current_skill_connection()
    token = _connection.set(connection)
    try:
        yield request, model
    finally:
        _connection.reset(token)


def _reject_prefix_connections(messages) -> None:
    # Connection transport is metadata-only: a user prefix is persisted as input.
    for message in messages or []:
        content = message.get("content") if isinstance(message, Mapping) else None
        texts = (
            [content]
            if isinstance(content, str)
            else [part.get("text", "") for part in content or [] if isinstance(part, Mapping)]
            if isinstance(content, list)
            else []
        )
        for text in texts:
            match = re.match(
                r"^\s*\[SKILL_EVAL_CONTROL\]\s*(.*?)\s*\[/SKILL_EVAL_CONTROL\]", text, re.S
            )
            if not match:
                continue
            try:
                control = json.loads(match[1])
            except (ValueError, TypeError):
                continue
            source = control.get("skill_source") if isinstance(control, Mapping) else None
            if isinstance(source, Mapping) and (
                CONNECTION_FIELDS | {"ak", "sk", "token", "credentials"}
            ).intersection(source):
                raise ValueError(
                    "Skill connection fields must use request metadata, not message prefixes"
                )


def transient_skill_connection(function):
    """Apply the transient boundary to async conversation entrypoints.

    For streams reset before yielding, so a suspended/abandoned generator never
    leaves credentials in its consumer task. Underlying execution tasks retain
    their own inherited context until completion or cancellation.
    """

    @wraps(function)
    async def call(*args, **kwargs):
        _reject_prefix_connections(kwargs.get("messages"))
        with skill_connection_scope(
            kwargs.get("request_metadata"), kwargs.get("model_metadata")
        ) as (request, model):
            sanitized = dict(kwargs)
            for key, value in (("request_metadata", request), ("model_metadata", model)):
                if key in kwargs:
                    sanitized[key] = value
            return await function(*args, **sanitized)

    @wraps(function)
    async def stream(*args, **kwargs):
        _reject_prefix_connections(kwargs.get("messages"))
        with skill_connection_scope(
            kwargs.get("request_metadata"), kwargs.get("model_metadata")
        ) as (request, model):
            connection = current_skill_connection()
            sanitized = dict(kwargs)
            for key, value in (("request_metadata", request), ("model_metadata", model)):
                if key in kwargs:
                    sanitized[key] = value
            iterator = function(*args, **sanitized)
        try:
            while True:
                token = _connection.set(connection)
                try:
                    item = await anext(iterator)
                except StopAsyncIteration:
                    break
                finally:
                    _connection.reset(token)
                yield item
        finally:
            token = _connection.set(connection)
            try:
                await iterator.aclose()
            finally:
                _connection.reset(token)

    return stream if inspect.isasyncgenfunction(function) else call

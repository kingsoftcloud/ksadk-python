from __future__ import annotations

import abc
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional


def generate_id() -> str:
    return uuid.uuid4().hex[:16]


def normalize_timestamp(value: Any, default: Optional[float] = None) -> float:
    if value is None:
        return default if default is not None else time.time()
    if isinstance(value, (int, float)):
        numeric = float(value)
        if numeric > 1_000_000_000_000:
            return numeric / 1000.0
        return numeric
    if isinstance(value, datetime):
        return value.timestamp()
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return default if default is not None else time.time()
        try:
            numeric = float(raw)
            return normalize_timestamp(numeric, default=default)
        except ValueError:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            return dt.timestamp()
    return default if default is not None else time.time()


@dataclass
class SessionEvent:
    id: str = field(default_factory=generate_id)
    session_id: str = ""
    author: str = ""
    event_type: str = ""
    content: dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)
    state_delta: dict[str, Any] = field(default_factory=dict)
    seq_id: int = 0
    invocation_id: Optional[str] = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(
        cls,
        payload: dict[str, Any],
        *,
        session_id: str = "",
        seq_id: int = 0,
    ) -> "SessionEvent":
        base_keys = {
            "id",
            "session_id",
            "sessionId",
            "author",
            "event_type",
            "eventType",
            "content",
            "timestamp",
            "state_delta",
            "stateDelta",
            "seq_id",
            "seqId",
            "invocation_id",
            "invocationId",
            "metadata",
        }
        metadata = dict(payload.get("metadata") or {})
        metadata.update({key: value for key, value in payload.items() if key not in base_keys})
        return cls(
            id=str(payload.get("id") or generate_id()),
            session_id=str(
                payload.get("session_id")
                or payload.get("sessionId")
                or session_id
                or ""
            ),
            author=str(payload.get("author") or ""),
            event_type=str(
                payload.get("event_type")
                or payload.get("eventType")
                or _infer_event_type(payload)
            ),
            content=dict(payload.get("content") or {}),
            timestamp=normalize_timestamp(payload.get("timestamp")),
            state_delta=dict(payload.get("state_delta") or payload.get("stateDelta") or {}),
            seq_id=int(payload.get("seq_id") or payload.get("seqId") or seq_id or 0),
            invocation_id=payload.get("invocation_id") or payload.get("invocationId"),
            metadata=metadata,
        )

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": self.id,
            "session_id": self.session_id,
            "author": self.author,
            "event_type": self.event_type,
            "content": self.content,
            "timestamp": self.timestamp,
            "state_delta": self.state_delta,
            "seq_id": self.seq_id,
            "metadata": self.metadata,
        }
        if self.invocation_id:
            payload["invocation_id"] = self.invocation_id
        return payload

    def to_legacy_dict(self) -> dict[str, Any]:
        payload = dict(self.metadata)
        payload.update(
            {
                "id": self.id,
                "author": self.author,
                "invocationId": self.invocation_id,
                "content": self.content,
                "timestamp": int(self.timestamp * 1000),
            }
        )
        if self.state_delta:
            payload["stateDelta"] = self.state_delta
        if self.event_type:
            payload["eventType"] = self.event_type
        return payload


@dataclass
class SessionState:
    scope: str
    agent_id: str
    user_id: str = ""
    session_id: str = ""
    state: dict[str, Any] = field(default_factory=dict)
    version: int = 0
    updated_at: float = field(default_factory=time.time)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "SessionState":
        return cls(
            scope=str(payload.get("scope") or "session"),
            agent_id=str(payload.get("agent_id") or ""),
            user_id=str(payload.get("user_id") or ""),
            session_id=str(payload.get("session_id") or ""),
            state=dict(payload.get("state") or {}),
            version=int(payload.get("version") or 0),
            updated_at=normalize_timestamp(payload.get("updated_at")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "scope": self.scope,
            "agent_id": self.agent_id,
            "user_id": self.user_id,
            "session_id": self.session_id,
            "state": self.state,
            "version": self.version,
            "updated_at": self.updated_at,
        }


@dataclass
class Session:
    id: str = field(default_factory=generate_id)
    agent_id: str = ""
    user_id: str = ""
    title: str = ""
    title_source: str = ""
    summary: str = ""
    first_prompt: str = ""
    last_prompt: str = ""
    state: dict[str, Any] = field(default_factory=dict)
    events: list[SessionEvent] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    version: int = 0

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "Session":
        events = [
            item if isinstance(item, SessionEvent) else SessionEvent.from_dict(item)
            for item in payload.get("events", [])
        ]
        return cls(
            id=str(payload.get("id") or generate_id()),
            agent_id=str(
                payload.get("agent_id")
                or payload.get("app_name")
                or payload.get("appName")
                or ""
            ),
            user_id=str(payload.get("user_id") or payload.get("userId") or ""),
            title=str(payload.get("title") or payload.get("Title") or ""),
            title_source=str(payload.get("title_source") or payload.get("TitleSource") or ""),
            summary=str(payload.get("summary") or payload.get("Summary") or ""),
            first_prompt=str(payload.get("first_prompt") or payload.get("FirstPrompt") or ""),
            last_prompt=str(payload.get("last_prompt") or payload.get("LastPrompt") or ""),
            state=dict(payload.get("state") or {}),
            events=events,
            created_at=normalize_timestamp(payload.get("created_at") or payload.get("createdAt")),
            updated_at=normalize_timestamp(payload.get("updated_at") or payload.get("updatedAt")),
            version=int(payload.get("version") or 0),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "agent_id": self.agent_id,
            "user_id": self.user_id,
            "title": self.title,
            "title_source": self.title_source,
            "summary": self.summary,
            "first_prompt": self.first_prompt,
            "last_prompt": self.last_prompt,
            "state": self.state,
            "events": [event.to_dict() for event in self.events],
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "version": self.version,
        }

    def to_legacy_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "appName": self.agent_id,
            "userId": self.user_id,
            "title": self.title,
            "summary": self.summary,
            "events": [event.to_legacy_dict() for event in self.events],
            "state": self.state,
            "createdAt": self.created_at,
            "updatedAt": self.updated_at,
        }


def _infer_event_type(payload: dict[str, Any]) -> str:
    content = payload.get("content") or {}
    role = str(content.get("role") or "")
    author = str(payload.get("author") or "")
    parts = content.get("parts") or []
    for part in parts:
        if isinstance(part, dict) and part.get("functionCall"):
            return "tool_call"
        if isinstance(part, dict) and part.get("functionResponse"):
            return "tool_result"
    try:
        from ksadk.conversations.context import canonical_event_type
    except Exception:
        if role in {"assistant", "model"} or author in {"assistant", "model"}:
            return "assistant_message"
        return "user_message"
    return canonical_event_type(None, author=author, role=role)


class BaseSessionService(abc.ABC):
    @abc.abstractmethod
    async def create_session(
        self,
        agent_id: str,
        user_id: str,
        session_id: Optional[str] = None,
    ) -> Session:
        raise NotImplementedError

    @abc.abstractmethod
    async def get_session(self, session_id: str) -> Optional[Session]:
        raise NotImplementedError

    @abc.abstractmethod
    async def list_sessions(
        self,
        agent_id: str,
        user_id: Optional[str] = None,
        offset: Optional[int] = None,
        limit: Optional[int] = None,
    ) -> list[Session]:
        raise NotImplementedError

    @abc.abstractmethod
    async def count_sessions(
        self,
        agent_id: str,
        user_id: Optional[str] = None,
    ) -> int:
        raise NotImplementedError

    @abc.abstractmethod
    async def delete_session(self, session_id: str) -> bool:
        raise NotImplementedError

    @abc.abstractmethod
    async def update_session_metadata(
        self,
        session_id: str,
        *,
        title: Optional[str] = None,
        title_source: Optional[str] = None,
        summary: Optional[str] = None,
        first_prompt: Optional[str] = None,
        last_prompt: Optional[str] = None,
    ) -> Session:
        raise NotImplementedError

    @abc.abstractmethod
    async def append_event(self, session_id: str, event: SessionEvent) -> SessionEvent:
        raise NotImplementedError

    @abc.abstractmethod
    async def get_events(
        self,
        session_id: str,
        offset: Optional[int] = None,
        limit: Optional[int] = None,
    ) -> list[SessionEvent]:
        raise NotImplementedError

    @abc.abstractmethod
    async def count_events(self, session_id: str) -> int:
        raise NotImplementedError

    @abc.abstractmethod
    async def get_state(
        self,
        agent_id: str,
        user_id: Optional[str],
        session_id: Optional[str],
        scope: str = "session",
    ) -> Optional[SessionState]:
        raise NotImplementedError

    @abc.abstractmethod
    async def update_state(
        self,
        *,
        agent_id: str,
        user_id: Optional[str],
        session_id: Optional[str],
        scope: str,
        state_delta: dict[str, Any],
    ) -> SessionState:
        raise NotImplementedError

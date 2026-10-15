"""Host-scoped recall seam for the DSH memory.provider/v1 capability."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import Field, model_validator

from ksadk.memory.models import MemorySearchResult
from ksadk.plugins.bundle import ResolvedPluginBundle
from ksadk.plugins.contracts import PluginContractModel

MemoryScopeName = Literal["tenant", "workspace", "agent", "user"]


class MemoryPrincipal(PluginContractModel):
    tenant_id: str = Field(min_length=1)
    workspace_id: str = Field(min_length=1)
    agent_id: str = Field(min_length=1)
    user_id: str = Field(min_length=1)


class MemoryRecallRequest(PluginContractModel):
    """Bounded candidates; max_tokens grants the host's final projected recall budget."""

    principal: MemoryPrincipal
    query: str = Field(max_length=131_072)
    scopes: tuple[MemoryScopeName, ...]
    top_k: int = Field(strict=True, ge=1, le=64)
    max_tokens: int = Field(strict=True, ge=0)
    min_score: float = Field(ge=0, le=1, allow_inf_nan=False)


class MemoryRecordRequest(PluginContractModel):
    principal: MemoryPrincipal
    scopes: tuple[MemoryScopeName, ...]
    memory_id: str = Field(min_length=1, max_length=256)


class MemoryReceiptRequest(PluginContractModel):
    principal: MemoryPrincipal
    operation_key: str = Field(min_length=1, max_length=1024)
    request_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class MemoryUserObservation(PluginContractModel):
    """Host-selected canonical user input; never model-supplied tool arguments."""

    source_event_id: str = Field(min_length=1, max_length=2048)
    source_session_id: str = Field(min_length=1, max_length=256)
    content: str = Field(min_length=1, max_length=131072)
    source_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")

    @model_validator(mode="after")
    def original_digest(self):
        if self.source_digest != "sha256:" + hashlib.sha256(self.content.encode()).hexdigest():
            raise ValueError("Memory observation source digest differs from original content")
        return self


class MemoryMutationRequest(MemoryReceiptRequest):
    """Host-derived authority and provenance; never a model tool argument schema."""

    action: Literal["remember", "correct", "forget"]
    scope: MemoryScopeName
    content: str = Field(default="", max_length=8192)
    memory_id: str | None = Field(default=None, min_length=1, max_length=256)
    expected_version: int | None = Field(default=None, strict=True, ge=1)
    authority: Literal["explicit_user", "approved_call", "candidate"]
    source_session_id: str = Field(min_length=1)
    source_event_id: str = Field(min_length=1)
    source_goal_revision: int = Field(strict=True, ge=0)
    source_goal_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    # Eligible original inputs at this actual write boundary. The model cannot
    # choose this horizon; old prose must not undo a confirmed write/forget.
    source_user_observations: tuple[MemoryUserObservation, ...] = Field(
        default=(), exclude_if=lambda value: not value,
    )

    @model_validator(mode="after")
    def valid_action(self):
        if self.action == "remember":
            if self.memory_id is not None or self.expected_version is not None:
                raise ValueError("new memory must not supply an existing identity")
        elif self.memory_id is None or self.expected_version is None:
            raise ValueError("correct/forget require an exact memory_id and observed version")
        if self.action != "forget" and not self.content.strip():
            raise ValueError("remember/correct require content")
        if self.action == "forget" and self.content:
            raise ValueError("forget must not carry a replacement fact")
        return self


class MemoryFlushRequest(PluginContractModel):
    principal: MemoryPrincipal
    trigger: Literal["pre_compaction", "completed_turn"]
    observations: tuple[MemoryUserObservation, ...]


@runtime_checkable
class MemoryFlushSource(Protocol):
    async def memory_flush(
        self, bundle: ResolvedPluginBundle, request: MemoryFlushRequest,
    ) -> dict: ...


@runtime_checkable
class MemoryRecallSource(Protocol):
    async def memory_recall(
        self, bundle: ResolvedPluginBundle, request: MemoryRecallRequest,
    ) -> MemorySearchResult: ...


@runtime_checkable
class MemorySearchSource(Protocol):
    """Explicit scoped search, independent of automatic recall's enable switch.

    The same locked recall limits bound candidates and the host's projected
    result. This API does not grant exhaustive enumeration or a write.
    """

    async def memory_search(
        self, bundle: ResolvedPluginBundle, request: MemoryRecallRequest,
    ) -> MemorySearchResult: ...


@runtime_checkable
class MemoryMutationSource(Protocol):
    async def memory_read(
        self, bundle: ResolvedPluginBundle, request: MemoryRecordRequest,
    ): ...

    async def memory_mutate(
        self, bundle: ResolvedPluginBundle, request: MemoryMutationRequest,
    ) -> dict: ...

    async def memory_receipt(
        self, bundle: ResolvedPluginBundle, request: MemoryReceiptRequest,
    ) -> dict | None: ...


@dataclass(frozen=True)
class MemoryProviderBinding:
    plugin_id: str
    plugin_version: str
    slot: str
    runtime: Any


def _namespace(values: tuple[str, ...]) -> str:
    raw = json.dumps(values, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


def memory_db_path(state_root: Path, principal: MemoryPrincipal) -> Path:
    """One tenant database beneath the explicit host state root, never an env path."""
    return state_root / "memory-v1" / _namespace((principal.tenant_id,)) / "memory.sqlite"


def memory_scope_bindings(principal: MemoryPrincipal, scopes: tuple[MemoryScopeName, ...]):
    """Keep project identity stable; tenant sharing requires an explicit scope.

    The existing storage vocabulary calls tenant-wide records ``org``.
    User memory retains the SDK's Agent x User partition inside its workspace.
    """
    prefix = (principal.tenant_id, principal.workspace_id)
    identities = {
        "tenant": ("org", (principal.tenant_id,)),
        "workspace": ("workspace", prefix),
        "agent": ("agent", (*prefix, principal.agent_id)),
        "user": ("user", (*prefix, principal.agent_id, principal.user_id)),
    }
    return tuple(
        (identities[name][0], "native-memory-v1:" + _namespace((name, *identities[name][1])))
        for name in dict.fromkeys(scopes)
    )

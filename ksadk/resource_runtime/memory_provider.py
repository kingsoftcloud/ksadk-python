"""Bound structured AICP MemoryProvider; asynchronous writes use a separate receipt path."""

from __future__ import annotations

import hashlib
import json
import math
import threading
import time
from contextlib import contextmanager

from pydantic import Field

from ksadk.memory.adk.backends.sdk_ltm_backend import SdkLTMBackend
from ksadk.memory.models import (
    CoreMemoryRequest,
    MemoryCapabilities,
    MemoryDeleteRequest,
    MemoryDeleteResult,
    MemoryRecord,
    MemorySearchRequest,
    MemorySearchResult,
    UnsupportedMemoryOperation,
)
from ksadk.plugins.contracts import PluginContractModel
from ksadk.resource_runtime.contracts import InvocationIdentity, ResourceConfig
from ksadk.resource_runtime.errors import ResourceUpstreamAuthorizationError


class MemoryQuery(PluginContractModel):
    query: str = Field(strict=True, max_length=16000)


class MemoryWrite(PluginContractModel):
    content: str = Field(strict=True, min_length=1, max_length=8192)


class MemoryStatusQuery(PluginContractModel):
    operation_id: str = Field(strict=True, pattern=r"^[0-9a-f]{64}$")


class MemoryRecordSelector(PluginContractModel):
    memory_id: str = Field(strict=True, min_length=1, max_length=256)


class MemoryUpdate(MemoryRecordSelector, MemoryWrite):
    pass


class AicpMemoryProvider:
    """An explicit activation instance. Never resolves credentials or user IDs from env.

    The caller supplies an admitted backend and identity. This search adapter does
    not turn an asynchronous submission into the synchronous upsert contract.
    """

    def __init__(
        self, config: ResourceConfig, identity: InvocationIdentity, backend: SdkLTMBackend
    ):
        if config.binding.resource.kind != "memory-instance":
            raise ValueError("MemoryProvider requires a memory-instance binding")
        if not backend.access_key or not backend.secret_key:
            raise ValueError("MemoryProvider requires explicit signing credentials")
        if (
            backend._effective_memory_collection_id() != config.binding.resource.id
            or backend.region != config.binding.resource.region
        ):
            raise ValueError("Memory backend does not match the resource binding")
        self.config = config
        self.identity = identity
        self.partition = identity.memory_partition(config.binding.resource)
        self._backend = backend
        self._lock = threading.Lock()
        self._observed_ids: dict[str, None] = {}
        self._upstream_forbidden = False

    @contextmanager
    def _backend_call(self):
        """Keep observed authorization failures sticky for this activation."""
        with self._lock:
            if self._upstream_forbidden:
                raise ResourceUpstreamAuthorizationError()
            self._backend.last_http_status = None
            try:
                yield
            finally:
                if self._backend.last_http_status in {401, 403}:
                    self._upstream_forbidden = True
                    self._observed_ids.clear()
                    raise ResourceUpstreamAuthorizationError() from None

    def capabilities(self) -> MemoryCapabilities:
        # The SDK exposes a query operation but no advertised search-mode or CAS
        # guarantee. Do not invent capabilities from the presence of a method.
        return MemoryCapabilities(
            semantic_search=False,
            keyword_search=False,
            metadata_filter=False,
            versioned_update=False,
            hard_delete=False,
            ttl=False,
            max_record_chars=8192,
        )

    def search(self, request: MemorySearchRequest) -> MemorySearchResult:
        started = time.monotonic()

        def result(status, records=None, error=None, truncated=False):
            return MemorySearchResult(
                status=status,
                records=records or [],
                error_code=error,
                provider="aicp",
                latency_ms=int((time.monotonic() - started) * 1000),
                accounting_accuracy="estimated",
                truncated_by_budget=truncated,
            )

        if request.scopes != [("user", self.partition)]:
            return result("unauthorized", error="MEMORY_SCOPE_MISMATCH")
        if request.filters or request.as_of or request.memory_types or request.min_score != 0:
            return result("failed", error="MEMORY_SEARCH_POLICY_UNSUPPORTED")
        if (
            type(request.top_k) is not int
            or not 1 <= request.top_k <= 64
            or type(request.max_tokens) is not int
            or not 0 <= request.max_tokens <= 100000
            or not isinstance(request.query, str)
            or len(request.query) > 16000
        ):
            return result("failed", error="MEMORY_SEARCH_ARGUMENTS_INVALID")
        if request.max_tokens == 0:
            return result("ok", truncated=True)
        try:
            with self._backend_call():
                records = self._backend.search_records(
                    self.partition, request.query, request.top_k, strict=True
                )
            projected = []
            remaining = request.max_tokens
            truncated = len(records) > request.top_k
            for item in records[: request.top_k]:
                if (
                    item.user_id != self.partition
                    or item.metadata.get("AgentUserId", self.partition) != self.partition
                    or not item.memory_id
                    or item.score is not None
                    and not math.isfinite(item.score)
                ):
                    return result("failed", error="MEMORY_SEARCH_RESPONSE_INVALID")
                content = item.content[:8192]
                # Byte accounting is conservative for ordinary text tokenizers;
                # accuracy remains explicitly estimated in the public result.
                cost = len(content.encode("utf-8"))
                if cost > remaining:
                    truncated = True
                    break
                remaining -= cost
                truncated |= len(content) != len(item.content)
                projected.append(
                    MemoryRecord(
                        memory_id=item.memory_id,
                        tenant_id=self.identity.tenant_ref,
                        workspace_id="",
                        scope="user",
                        scope_id=self.partition,
                        memory_type="unknown",
                        content=content,
                        summary=content,
                        status="active",
                        confidence=None,
                        importance=None,
                        valid_from="",
                        valid_to="",
                        expires_at="",
                        source_session_id=item.session_id,
                        source_event_ids=[],
                        source_seq_range=None,
                        content_hash="sha256:" + hashlib.sha256(content.encode()).hexdigest(),
                        version=None,
                        metadata={"bindingId": self.config.binding.id, "score": item.score},
                        created_at=item.created_at or "",
                        updated_at=item.updated_at or "",
                    )
                )
            with self._lock:
                for record in projected:
                    self._observed_ids[record.memory_id] = None
                while len(self._observed_ids) > 256:
                    del self._observed_ids[next(iter(self._observed_ids))]
            return result("ok", projected, truncated=truncated)
        except ResourceUpstreamAuthorizationError:
            return result("unauthorized", error="RESOURCE_FORBIDDEN")
        except Exception:
            return result("failed", error="MEMORY_SEARCH_FAILED")

    def get(self, memory_id: str) -> MemoryRecord | None:
        raise UnsupportedMemoryOperation("AICP does not expose a bound get-by-ID contract")

    def submit(self, arguments: dict, *, operation_id: str) -> dict:
        """Host-approved submission; each operation has a separate extraction session.

        The broker supplies the durable operation ID, never a model parameter.
        A transport acknowledgment proves acceptance only, not indexing or IDs.
        """
        request = MemoryWrite.model_validate(arguments)
        if (
            not request.content.strip()
            or len(operation_id) != 64
            or any(char not in "0123456789abcdef" for char in operation_id)
        ):
            raise ValueError("MEMORY_WRITE_ARGUMENTS_INVALID")
        # AICP limits SessionId to 64 characters.  The broker operation ID is
        # already a 64-character lowercase hex digest, so use it directly.
        # Prefixing it made every real submission 73 characters long and the
        # service rejected the write before extraction could start.
        session_id = operation_id
        event = json.dumps({"role": "user", "parts": [{"text": request.content}]})
        with self._backend_call():
            try:
                accepted = self._backend.save_memory(
                    self.partition, [event], session_id=session_id, flush=True, strict=True
                )
            except Exception:
                accepted = False
        return {
            "status": "accepted_pending" if accepted else "unknown",
            "operationId": operation_id,
            "searchable": False,
            "errorCode": None if accepted else "MEMORY_WRITE_UNKNOWN",
        }

    def extraction_status(self, arguments: dict) -> dict:
        query = MemoryStatusQuery.model_validate(arguments)
        with self._backend_call():
            result = self._backend.get_extraction_status(
                user_id=self.partition,
                session_id=query.operation_id,
            )
        return {
            "operationId": query.operation_id,
            "extractionStatus": result.status,
            "searchable": False,
            "errorCode": result.error_code or None,
        }

    def mutate(self, operation: str, arguments: dict, *, operation_id: str) -> dict:
        """Post-approval management of IDs observed in this bound user partition.

        IDs are not authorization tokens: upstream receives the bound user on every
        request. Restart requires fresh search. No CAS, hard deletion, or retry is
        implied, and an ambiguous response permanently consumes the observed ID.
        """
        MemoryStatusQuery(operation_id=operation_id)
        if operation == "update_memory":
            query = MemoryUpdate.model_validate(arguments)
            if not query.content.strip():
                raise ValueError("MEMORY_WRITE_ARGUMENTS_INVALID")
        elif operation == "delete_memory":
            query = MemoryRecordSelector.model_validate(arguments)
        else:
            raise ValueError("Unsupported memory mutation")
        reply = {
            "operationId": operation_id,
            "memoryId": query.memory_id,
            "newMemoryId": "",
            "status": "unknown",
            "errorCode": "MEMORY_MUTATION_UNKNOWN",
        }
        with self._backend_call():
            if query.memory_id not in self._observed_ids:
                return {**reply, "status": "failed", "errorCode": "MEMORY_RECORD_NOT_OBSERVED"}
            del self._observed_ids[query.memory_id]
            try:
                if operation == "update_memory":
                    outcome = self._backend.update_memory(
                        user_id=self.partition,
                        memory_id=query.memory_id,
                        content=query.content,
                        strict=True,
                    )
                else:
                    outcome = self._backend.delete_memory(
                        user_id=self.partition,
                        memory_id=query.memory_id,
                        strict=True,
                    )
                expected = (
                    {"updated"} if operation == "update_memory" else {"deleted", "already_absent"}
                )
                if outcome.ok and outcome.status in expected:
                    new_id = outcome.new_memory_id if operation == "update_memory" else ""
                    if not isinstance(new_id, str) or len(new_id) > 256:
                        return reply
                    if new_id:
                        self._observed_ids[new_id] = None
                    return {
                        **reply,
                        "status": "succeeded",
                        "newMemoryId": new_id,
                        "errorCode": None,
                    }
                if outcome.status == "not_found":
                    return {**reply, "status": "failed", "errorCode": "MEMORY_RECORD_NOT_FOUND"}
            except Exception:
                pass
        return reply

    def upsert(self, record: MemoryRecord, *, expected_version: int | None) -> MemoryRecord:
        raise UnsupportedMemoryOperation(
            "AICP writes require an approved asynchronous operation receipt"
        )

    def delete(self, request: MemoryDeleteRequest) -> MemoryDeleteResult:
        raise UnsupportedMemoryOperation("AICP deletion requires an approved operation receipt")

    def list_core(self, request: CoreMemoryRequest) -> list[MemoryRecord]:
        raise UnsupportedMemoryOperation("AICP does not expose core-memory blocks")

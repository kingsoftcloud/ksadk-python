"""Scoped Native adapter over the existing local ArtifactStore."""

from __future__ import annotations

import hashlib

from ksadk.harness.artifact_store import ArtifactStore
from ksadk.harness.native.adapters import _snapshot
from ksadk.harness.native.ports import (
    ArtifactReadRequest,
    ArtifactReadResult,
    ArtifactRef,
    ArtifactWriteRequest,
)
from ksadk.harness.native.state import RunScope


class NativeArtifactStorePort:
    """Bind one local store to an authenticated scope without a second ledger."""

    def __init__(self, store: ArtifactStore, *, scope: RunScope) -> None:
        if not isinstance(store, ArtifactStore):
            raise ValueError("native artifact adapter requires ArtifactStore")
        self._store = store
        self._scope = _snapshot(scope)
        self._scope_key = hashlib.sha256(self._scope.model_dump_json().encode()).hexdigest()[:24]

    def _run_key(self, run_id: str) -> str:
        digest = hashlib.sha256(run_id.encode()).hexdigest()[:32]
        return f"{self._scope_key}-{digest}"

    @staticmethod
    def _ref(record) -> ArtifactRef:
        return ArtifactRef(
            uri=record.uri,
            content_hash=record.content_hash,
            size_bytes=record.bytes,
            mime_type=record.mime,
        )

    async def usage_bytes(self, scope: RunScope, run_id: str) -> int:
        """Count this Run's durable artifacts, including writes before a crash."""
        if _snapshot(scope) != self._scope:
            raise ValueError("artifact scope mismatch")
        return sum(record.bytes for record in self._store.list(self._run_key(run_id)))

    async def write(self, request: ArtifactWriteRequest) -> ArtifactRef:
        sealed = _snapshot(request)
        if sealed.scope != self._scope:
            raise ValueError("artifact scope mismatch")
        run_key = self._run_key(sealed.run_id)
        identity = hashlib.sha256(
            f"{sealed.operation_key}\0{sealed.tool_call_id}".encode()
        ).hexdigest()[:48]
        name = f"tool-result-{identity}"
        encoded = sealed.content.encode("utf-8")
        existing = self._store.latest(run_key, name)
        if existing is not None:
            ref = self._ref(existing)
            if (
                ref.content_hash != "sha256:" + hashlib.sha256(encoded).hexdigest()
                or ref.size_bytes != len(encoded)
                or ref.mime_type != sealed.mime_type
                or self._store.read(existing) != encoded
            ):
                raise ValueError("artifact operation identity conflict")
            return ref
        record = self._store.save(
            run_id=run_key,
            name=name,
            content=encoded,
            mime=sealed.mime_type,
        )
        return self._ref(record)

    async def read(self, request: ArtifactReadRequest) -> ArtifactReadResult:
        sealed = _snapshot(request)
        if sealed.scope != self._scope:
            raise ValueError("artifact scope mismatch")
        # Current-turn writes use a scope-prefixed run key. Inherited refs are
        # allowed only when
        # the runtime placed their exact typed ref in available_artifacts; this
        # adapter still requires the same authenticated scope prefix.
        prefix = f"artifact://{self._scope_key}-"
        if not sealed.ref.uri.startswith(prefix):
            raise ValueError("artifact reference is outside the bound scope")
        records = [
            record
            for record in self._store.list(
                sealed.ref.uri[len("artifact://") :].split("/", 1)[0]
            )
            if record.uri == sealed.ref.uri
        ]
        if len(records) != 1 or self._ref(records[0]) != sealed.ref:
            raise ValueError("artifact reference is unknown or changed")
        encoded = self._store.read(records[0])
        if len(encoded) != sealed.ref.size_bytes or (
            "sha256:" + hashlib.sha256(encoded).hexdigest() != sealed.ref.content_hash
        ):
            raise ValueError("artifact content integrity mismatch")
        text = encoded.decode("utf-8")
        if sealed.offset_chars > len(text):
            raise ValueError("artifact offset is beyond content")
        content = text[sealed.offset_chars : sealed.offset_chars + sealed.limit_chars]
        next_offset = sealed.offset_chars + len(content)
        return ArtifactReadResult(
            ref=sealed.ref,
            content=content,
            offset_chars=sealed.offset_chars,
            next_offset_chars=next_offset,
            complete=next_offset == len(text),
        )


__all__ = ["NativeArtifactStorePort"]

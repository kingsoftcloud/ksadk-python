"""Fixed Server-only byte transport for prepared Teams materials/artifacts."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Mapping
from urllib.parse import quote

import httpx
from pydantic import TypeAdapter

from ksadk.kernel.teams_materials import CHUNK_BYTES, ReadyMaterial
from ksadk.plugins.teams.cloud_contracts import MAX_FILE_BYTES, Digest, Identifier
from ksadk.plugins.teams.errors import TeamsError
from ksadk.plugins.teams.transport import TeamsHTTPClient

MAX_JSON_BYTES = 2 * 1024 * 1024


@dataclass(frozen=True)
class MaterialHTTPAuthorization:
    headers: Mapping[str, str] = field(repr=False)
    node_command_id: str | None = None


def _segment(value):
    TypeAdapter(Identifier).validate_python(value)
    if value in {".", ".."} or any(c in value for c in "/\\?#%"):
        raise ValueError("material endpoint requires one fixed identifier segment")
    return quote(value, safe="")


def _failure(code="material_transport_unavailable", status=503):
    return TeamsError(code, "材料传输未完成，请核对原操作后重试", status=status)


class TeamsHTTPMaterialTransport:
    """Use configured ``.../teams`` base_url, with fresh authorization per call.

    ``authorization(context, operation)`` is async and returns
    MaterialHTTPAuthorization. Node mode accepts only X-Teams-Node-Token and
    optionally the original prepare nodeCommandId. Runtime mode accepts exactly
    one X-Teams-Execution-Permit (prepare reads) or X-Teams-Callback-Permit. No
    owner credentials, redirects or response-supplied storage URLs are used.
    """

    def __init__(self, base_url, *, authorization, node_id=None, transport=None):
        self.client = TeamsHTTPClient(base_url, transport=transport)
        self.authorization = authorization
        self.node_id = node_id
        self.prefix = "/runtime" if node_id is None else "/nodes/" + _segment(node_id)

    async def close(self):
        await self.client.close()

    async def _scope(self, context, operation):
        auth = await self.authorization(context, operation)
        if not isinstance(auth, MaterialHTTPAuthorization):
            raise _failure("material_authorization_invalid", 403)
        headers = {}
        for name, value in auth.headers.items():
            if not isinstance(name, str) or not isinstance(value, str) or name.lower() in headers:
                raise _failure("material_authorization_invalid", 403)
            if (
                not value
                or len(value.encode()) > 16384
                or any(ord(c) < 32 or ord(c) > 126 for c in value)
            ):
                raise _failure("material_authorization_invalid", 403)
            headers[name.lower()] = value
        allowed = (
            ({"x-teams-node-token"},)
            if self.node_id
            else ({"x-teams-execution-permit"}, {"x-teams-callback-permit"})
        )
        if set(headers) not in allowed or (
            operation != "read_material" and "x-teams-execution-permit" in headers
        ):
            raise _failure("material_authorization_invalid", 403)
        params = {"contextRef": context.context_ref}
        if auth.node_command_id is not None:
            if self.node_id is None:
                raise _failure("material_authorization_invalid", 403)
            TypeAdapter(Identifier).validate_python(auth.node_command_id)
            params["nodeCommandId"] = auth.node_command_id
        headers["Accept-Encoding"] = "identity"
        return headers, params

    @asynccontextmanager
    async def _response(
        self, context, operation, method, path, *, content=None, content_length=None
    ):
        headers, params = await self._scope(context, operation)
        if content_length is not None:
            headers.update(
                {"Content-Type": "application/octet-stream", "Content-Length": str(content_length)}
            )
        elif content is not None:
            headers["Content-Type"] = "application/json"
        try:
            async with self.client.http.stream(
                method,
                self.client.base_url + self.prefix + path,
                params=params,
                headers=headers,
                content=content,
                follow_redirects=False,
            ) as response:
                if not 200 <= response.status_code < 300:
                    # Do not echo remote bodies or Location values: either may
                    # contain credentials. A failed mutation is never retried.
                    code = "material_request_rejected"
                    if (
                        response.status_code == 409
                        and response.headers.get("Content-Encoding", "identity").lower()
                        == "identity"
                    ):
                        data = bytearray()
                        async for chunk in response.aiter_bytes(4096):
                            if len(data) + len(chunk) > 4096:
                                data.clear()
                                break
                            data.extend(chunk)
                        try:
                            body = json.loads(data)
                        except (ValueError, UnicodeDecodeError):
                            body = None
                        # This one public constant distinguishes the original
                        # submit-report race. Never expose arbitrary server data.
                        if isinstance(body, dict) and any(
                            isinstance(body.get(key), dict)
                            and body[key].get("code") == "original_run_unverified"
                            for key in ("error", "detail")
                        ):
                            code = "original_run_unverified"
                    raise _failure(
                        code,
                        response.status_code if response.status_code >= 400 else 502,
                    )
                if response.headers.get("Content-Encoding", "identity").lower() != "identity":
                    raise _failure("material_response_encoding_invalid", 502)
                yield response
        except httpx.RequestError:
            raise _failure() from None

    async def _json(self, context, operation, method, path, body=None, *, content=None, size=None):
        if body is not None:
            content = json.dumps(body, ensure_ascii=True, separators=(",", ":")).encode()
            if len(content) > MAX_JSON_BYTES:
                raise _failure("material_request_too_large", 413)
        async with self._response(
            context, operation, method, path, content=content, content_length=size
        ) as response:
            data = bytearray()
            async for chunk in response.aiter_bytes(CHUNK_BYTES):
                data.extend(chunk)
                if len(data) > MAX_JSON_BYTES:
                    raise _failure("material_response_too_large", 502)
            try:
                value = json.loads(data)
            except (UnicodeDecodeError, ValueError):
                raise _failure("material_response_invalid", 502) from None
            if not isinstance(value, dict):
                raise _failure("material_response_invalid", 502)
            return value

    async def fetch_manifest(self, context, material_ref):
        if context.material_manifest_ref != material_ref:
            raise _failure("material_scope_mismatch", 403)
        result = await self._json(
            context, "read_material", "GET", "/materials/" + _segment(material_ref)
        )
        return ReadyMaterial.model_validate(result).model_dump(mode="json", exclude_none=True)

    @asynccontextmanager
    async def open_blob(self, context, material_ref, fingerprint):
        if context.material_manifest_ref != material_ref:
            raise _failure("material_scope_mismatch", 403)
        TypeAdapter(Digest).validate_python(fingerprint)
        path = "/materials/" + _segment(material_ref) + "/blobs/" + _segment(fingerprint)
        async with self._open_bytes(context, path) as chunks:
            yield chunks

    @asynccontextmanager
    async def open_artifact(self, context, group_id, artifact_id):
        if context.ref.groupId != group_id:
            raise _failure("artifact_scope_mismatch", 403)
        path = "/groups/" + _segment(group_id) + "/artifacts/" + _segment(artifact_id) + "/content"
        async with self._open_bytes(context, path) as chunks:
            yield chunks

    @asynccontextmanager
    async def _open_bytes(self, context, path):
        async with self._response(context, "read_material", "GET", path) as response:

            async def chunks():
                size = 0
                async for chunk in response.aiter_bytes(CHUNK_BYTES):
                    size += len(chunk)
                    if size > MAX_FILE_BYTES:
                        raise _failure("material_response_too_large", 413)
                    yield chunk

            yield chunks()

    async def create_artifact(self, context, body):
        if set(body) != {"name", "mediaType", "sizeBytes", "digest", "idempotencyKey"}:
            raise _failure("artifact_request_invalid", 422)
        return await self._json(context, "create_artifact", "POST", "/artifacts", body)

    async def upload_artifact(self, context, artifact_id, chunks, *, size_bytes):
        if type(size_bytes) is not int or not 0 <= size_bytes <= MAX_FILE_BYTES:
            raise _failure("artifact_size_invalid", 413)

        async def bounded():
            total = 0
            async for chunk in chunks:
                if not isinstance(chunk, bytes) or len(chunk) > CHUNK_BYTES:
                    raise _failure("artifact_bytes_invalid", 422)
                total += len(chunk)
                if total > size_bytes:
                    raise _failure("artifact_size_invalid", 422)
                yield chunk
            if total != size_bytes:
                raise _failure("artifact_size_invalid", 422)

        return await self._json(
            context,
            "upload_artifact",
            "PUT",
            "/artifacts/" + _segment(artifact_id) + "/content",
            content=bounded(),
            size=size_bytes,
        )

    async def finalize_artifact(self, context, artifact_id, *, idempotency_key):
        return await self._json(
            context,
            "finalize_artifact",
            "POST",
            "/artifacts/" + _segment(artifact_id) + "/finalize",
            {"idempotencyKey": idempotency_key},
        )

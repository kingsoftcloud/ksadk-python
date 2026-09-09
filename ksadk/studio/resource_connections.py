"""Credential-reference declarations for local Studio platform resources.

These are user configuration, never proof of upstream identity or grants. Build
admission must verify the declared principal using the actual platform authority.
"""

from __future__ import annotations

import hashlib
from contextlib import contextmanager
from typing import Annotated

try:
    import fcntl
except ImportError:  # Windows can use Studio without the Unix resource host.
    fcntl = None  # type: ignore[assignment]

from pydantic import ConfigDict, Field, SecretStr, model_validator

from ksadk.plugins.contracts import PluginContractModel
from ksadk.resource_runtime.snapshots import ConnectionTarget
from ksadk.studio.errors import StudioError
from ksadk.studio.model_client import CredentialResolver
from ksadk.studio.workspace import Workspace

SecretReference = Annotated[str, Field(strict=True, pattern=r"^env://[A-Z0-9_]+$", max_length=256)]


class ResourceCredentialReferences(PluginContractModel):
    access_key_ref: SecretReference | None = None
    secret_key_ref: SecretReference | None = None
    session_token_ref: SecretReference | None = None
    token_ref: SecretReference | None = None


class ResourceConnectionDeclaration(PluginContractModel):
    label: str = Field(strict=True, min_length=1, max_length=128)
    target: ConnectionTarget
    credentials: ResourceCredentialReferences

    @model_validator(mode="after")
    def matching_auth(self):
        if len(self.target.endpoint) > 2048:
            raise ValueError("Resource endpoint is too long")
        refs = self.credentials
        present = {name for name, value in refs.model_dump().items() if value is not None}
        expected = {
            "signed": {"access_key_ref", "secret_key_ref"},
            "sts": {"access_key_ref", "secret_key_ref", "session_token_ref"},
            "token": {"token_ref"},
        }[self.target.auth_mode]
        if present != expected:
            raise ValueError("Credential references must match the declared authentication mode")
        return self


class ResourceConnectionRecord(ResourceConnectionDeclaration):
    revision: int = Field(strict=True, ge=1)


class ResolvedResourceCredentials(PluginContractModel):
    model_config = ConfigDict(hide_input_in_errors=True)
    access_key: SecretStr | None = Field(default=None, repr=False)
    secret_key: SecretStr | None = Field(default=None, repr=False)
    session_token: SecretStr | None = Field(default=None, repr=False)
    token: SecretStr | None = Field(default=None, repr=False)


class ResourceConnectionRepository:
    MAX_CONNECTIONS = 256

    def __init__(self, workspace: Workspace, credentials: CredentialResolver):
        self.workspace = workspace
        self.credentials = credentials
        self.root = workspace.resolve(".agentkit/resource-connections")

    @contextmanager
    def _locked(self):
        if fcntl is None:
            raise StudioError(
                "RESOURCE_CONNECTION_PLATFORM_UNSUPPORTED",
                "当前平台不支持资源连接锁，请在 Unix 宿主中使用平台资源插件",
                status_code=501,
            )
        self.workspace.resolve(self.root)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.workspace.resolve(self.root / ".lock").open("a+b") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def _path(self, reference: str):
        digest = hashlib.sha256(reference.encode()).hexdigest()
        return self.workspace.resolve(self.root / f"{digest}.json")

    def get(self, reference: str) -> ResourceConnectionRecord:
        path = self._path(reference)
        if not path.is_file():
            raise StudioError("RESOURCE_CONNECTION_NOT_FOUND", "资源连接不存在", status_code=404)
        try:
            if path.stat().st_size > 16384:
                raise ValueError("Oversized connection")
            record = ResourceConnectionRecord.model_validate_json(path.read_bytes())
            if record.target.connection_ref != reference:
                raise ValueError("Connection reference mismatch")
            return record
        except (OSError, ValueError) as error:
            raise StudioError(
                "RESOURCE_CONNECTION_INVALID",
                "资源连接记录损坏",
                status_code=409,
            ) from error

    def list(self) -> list[ResourceConnectionRecord]:
        with self._locked():
            paths = sorted(self.root.glob("*.json"))
            if len(paths) > self.MAX_CONNECTIONS:
                raise StudioError("RESOURCE_CONNECTION_LIMIT", "资源连接数量超限", status_code=409)
            records = []
            for path in paths:
                try:
                    if path.stat().st_size > 16384:
                        raise ValueError("Oversized connection")
                    candidate = ResourceConnectionRecord.model_validate_json(
                        self.workspace.resolve(path).read_bytes()
                    )
                    if self._path(candidate.target.connection_ref) != self.workspace.resolve(path):
                        raise ValueError("Connection filename does not match reference")
                    records.append(self.get(candidate.target.connection_ref))
                except (OSError, ValueError) as error:
                    raise StudioError(
                        "RESOURCE_CONNECTION_INVALID",
                        "资源连接记录损坏",
                        status_code=409,
                    ) from error
            return records

    def save(
        self,
        declaration: ResourceConnectionDeclaration,
        *,
        expected_revision: int,
    ) -> ResourceConnectionRecord:
        declaration = ResourceConnectionDeclaration.model_validate(declaration.model_dump())
        if type(expected_revision) is not int or expected_revision < 0:
            raise ValueError("Expected revision must be a nonnegative integer")
        with self._locked():
            path = self._path(declaration.target.connection_ref)
            current = self.get(declaration.target.connection_ref) if path.exists() else None
            if (current.revision if current else 0) != expected_revision:
                raise StudioError(
                    "RESOURCE_CONNECTION_REVISION_CONFLICT",
                    "资源连接已变更，请刷新后重试",
                    status_code=409,
                )
            if current is None and len(list(self.root.glob("*.json"))) >= self.MAX_CONNECTIONS:
                raise StudioError("RESOURCE_CONNECTION_LIMIT", "资源连接数量超限", status_code=409)
            record = ResourceConnectionRecord(
                **declaration.model_dump(),
                revision=expected_revision + 1,
            )
            payload = record.model_dump_json(by_alias=True)
            if len(payload.encode()) > 16384:
                raise ValueError("Resource connection declaration is too large")
            self.workspace.atomic_write_text(path, payload + "\n")
            return record

    def resolve_credentials(
        self,
        expected: ConnectionTarget,
        *,
        expected_revision: int | None = None,
    ) -> ResolvedResourceCredentials:
        """Host-only secret lookup after admission, not an authorization decision.

        Explicit references use the existing resolver without model-key aliases.
        No API route returns these values. Principal verification remains required.
        """
        with self._locked():
            record = self.get(expected.connection_ref)
            if record.target != expected or (
                expected_revision is not None and record.revision != expected_revision
            ):
                raise StudioError(
                    "RESOURCE_CONNECTION_CHANGED",
                    "资源连接与 Build 不一致，需要重新构建",
                    status_code=409,
                )
            values = {
                name.removesuffix("_ref"): SecretStr(
                    self.credentials.resolve(reference, allow_aliases=False)
                )
                for name, reference in record.credentials.model_dump().items()
                if reference is not None
            }
            return ResolvedResourceCredentials(**values)

    def create_control_client(self, expected: ConnectionTarget, *, region: str):
        """Explicit signed management client; does not perform resource admission.

        Browser-declared tenant/principal labels are not forwarded as identity
        headers. The upstream gateway must establish identity from authentication.
        Token/STS control-plane contracts require separate verified adapters.
        """
        from ksadk.api.client import AgentEngineClient

        if expected.auth_mode != "signed":
            raise StudioError(
                "RESOURCE_CONTROL_AUTH_UNSUPPORTED", "当前控制面适配仅支持签名连接",
                status_code=422,
            )
        credentials = self.resolve_credentials(expected)
        return AgentEngineClient(
            base_url=expected.endpoint, region=region,
            access_key=credentials.access_key.get_secret_value(),
            secret_key=credentials.secret_key.get_secret_value(),
            allow_env_fallback=False, timeout=10.0,
        )

"""Host-owned platform resource admission for explicit signed connections.

The first supported slice verifies a sub-account with IAM and proves read access
to one exact knowledge-base binding. Browser declarations are comparisons only;
they never become authority by being well formed or present in a catalogue.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Literal
from urllib.parse import urlsplit, urlunsplit

import requests
from pydantic import Field, model_validator

from ksadk.knowledge_base.client import KnowledgeBaseClient
from ksadk.memory.adk.backends.sdk_ltm_backend import SdkLTMBackend
from ksadk.plugins.contracts import PluginContractModel
from ksadk.resource_runtime.contracts import Identifier, ResourceConfig, ResourceRef
from ksadk.resource_runtime.ipc import ResourceOperation
from ksadk.skills.service_client import SkillServiceClient
from ksadk.studio.errors import StudioError
from ksadk.studio.resource_connections import (
    ResolvedResourceCredentials,
    ResourceConnectionRepository,
)

_IAM_KRN = re.compile(r"^krn:ksc:iam::([^:]+):user/([^/]+)$")
_MAX_AUTHORITY_RESPONSE_BYTES = 1024 * 1024
_KSYUN_INTERNAL_HTTP_HOSTS = frozenset(
    {
        "iam.inner.api.ksyun.com",
        "iam.internal.api.ksyun.com",
        "aicp.inner.api.ksyun.com",
        "aicp.internal.api.ksyun.com",
    }
)


def _canonical_endpoint(
    value: str,
    *,
    allow_loopback_http: bool,
    allow_ksyun_internal_http: bool,
) -> str:
    if any(char.isspace() for char in value) or "\\" in value:
        raise ValueError("Resource authority endpoint is invalid")
    parsed = urlsplit(value)
    parsed.port
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Resource authority endpoint must be an explicit HTTP service URL")
    host = parsed.hostname.encode("idna").decode("ascii").lower()
    if "%" in host:
        raise ValueError("Resource authority endpoint hostname is invalid")
    loopback_http = allow_loopback_http and host in {"127.0.0.1", "::1", "localhost"}
    internal_http = (
        parsed.scheme == "http"
        and allow_ksyun_internal_http
        and host in _KSYUN_INTERNAL_HTTP_HOSTS
    )
    if parsed.scheme != "https" and not (loopback_http or internal_http):
        raise ValueError("Resource authority endpoints require HTTPS")
    if internal_http and (parsed.port not in {None, 80} or parsed.path.rstrip("/")):
        raise ValueError("Ksyun internal HTTP endpoints must use the service root")
    if ":" in host:
        host = f"[{host}]"
    if parsed.port is not None and (parsed.scheme, parsed.port) not in {
        ("http", 80),
        ("https", 443),
    }:
        host += f":{parsed.port}"
    return urlunsplit((parsed.scheme, host, parsed.path.rstrip("/"), "", ""))


class ResourceAuthorityPolicy(PluginContractModel):
    """Trusted host configuration; it is never populated from a Studio request."""

    iam_endpoint: str
    iam_region: Identifier = "cn-beijing-6"
    allowed_data_endpoints: tuple[str, ...] = Field(min_length=1, max_length=16)
    allowed_regions: tuple[Identifier, ...] = Field(min_length=1, max_length=32)
    timeout_seconds: float = Field(default=10.0, gt=0, le=30)
    grant_ttl_seconds: int = Field(default=60, strict=True, ge=5, le=600)
    allow_loopback_http_for_tests: bool = Field(default=False, strict=True)
    allow_ksyun_internal_http: bool = Field(default=False, strict=True)

    @model_validator(mode="after")
    def trusted_targets(self) -> ResourceAuthorityPolicy:
        iam = _canonical_endpoint(
            self.iam_endpoint,
            allow_loopback_http=self.allow_loopback_http_for_tests,
            allow_ksyun_internal_http=self.allow_ksyun_internal_http,
        )
        data = tuple(
            _canonical_endpoint(
                endpoint,
                allow_loopback_http=self.allow_loopback_http_for_tests,
                allow_ksyun_internal_http=self.allow_ksyun_internal_http,
            )
            for endpoint in self.allowed_data_endpoints
        )
        if len(data) != len(set(data)) or len(self.allowed_regions) != len(
            set(self.allowed_regions)
        ):
            raise ValueError("Resource authority targets must be unique")
        object.__setattr__(self, "iam_endpoint", iam)
        object.__setattr__(self, "allowed_data_endpoints", data)
        return self


class VerifiedResourceAuthority(PluginContractModel):
    """Short-lived, credential-free evidence produced only by the trusted host."""

    schema_version: Literal[1] = 1
    connection_ref: Identifier
    connection_revision: int = Field(strict=True, ge=1)
    tenant_ref: Identifier
    resource_principal_ref: Identifier
    resource: ResourceRef
    allowed_operations: tuple[ResourceOperation, ...] = Field(min_length=1)
    issuer_endpoint: str
    issuer_region: Identifier
    data_endpoint: str
    observed_at: datetime
    expires_at: datetime
    request_id: str = Field(default="", max_length=256)

    @property
    def digest(self) -> str:
        payload = self.model_dump(by_alias=True, mode="json")
        raw = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
        return "sha256:" + hashlib.sha256(raw).hexdigest()


@dataclass(frozen=True)
class AdmittedResourceAccess:
    """Runtime-only authority proof paired with the exact verified credentials."""

    authority: VerifiedResourceAuthority
    credentials: ResolvedResourceCredentials = field(repr=False)


@dataclass(frozen=True)
class _VerifiedIamIdentity:
    tenant_ref: str
    principal_ref: str


class _HardenedSdkTransport:
    """Use the SDK's request serialization/signing with a closed HTTP transport."""

    def __init__(self, endpoint: str, *, timeout: float):
        self.endpoint = endpoint
        self.timeout = timeout

    def send_request(self, request: Any):
        from ksyun.common.http.request import ResponseInternal  # type: ignore[import-untyped]

        url = self.endpoint
        path = str(request.uri or "")
        if path not in {"", "/"}:
            url += "/" + path.lstrip("/")
        if request.uri_params:
            url += "?" + request.uri_params
        with requests.Session() as session:
            session.trust_env = False
            with session.request(
                method=request.method,
                url=url,
                data=request.data,
                headers=dict(request.header),
                auth=request.auth,
                timeout=self.timeout,
                verify=True,
                allow_redirects=False,
                stream=True,
            ) as response:
                if 300 <= response.status_code < 400:
                    raise RuntimeError("RESOURCE_AUTHORITY_REDIRECT_REFUSED")
                declared_length = response.headers.get("Content-Length")
                if declared_length and int(declared_length) > _MAX_AUTHORITY_RESPONSE_BYTES:
                    raise RuntimeError("RESOURCE_AUTHORITY_RESPONSE_TOO_LARGE")
                chunks = []
                size = 0
                for chunk in response.iter_content(chunk_size=65536):
                    size += len(chunk)
                    if size > _MAX_AUTHORITY_RESPONSE_BYTES:
                        raise RuntimeError("RESOURCE_AUTHORITY_RESPONSE_TOO_LARGE")
                    chunks.append(chunk)
                content = b"".join(chunks)
                return ResponseInternal(
                    status=response.status_code,
                    header=dict(response.headers),
                    data=content.decode("utf-8", errors="strict"),
                )


def _response_object(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        if len(value.encode()) > _MAX_AUTHORITY_RESPONSE_BYTES:
            raise ValueError("oversized response")
        value = json.loads(value)
    if not isinstance(value, dict):
        raise ValueError("invalid authority response")
    metadata = value.get("ResponseMetadata", {})
    if not isinstance(metadata, dict) or value.get("Error") or metadata.get("Error"):
        raise ValueError("authority rejected the request")
    code = value.get("Code")
    if "Code" in value and not (
        (type(code) is int and code in {0, 200})
        or (type(code) is str and code in {"0", "200"})
    ):
        raise ValueError("authority rejected the request")
    return value


def _credential_values(credentials: ResolvedResourceCredentials) -> tuple[str, str, str, str]:
    def reveal(value: Any) -> str:
        return value.get_secret_value() if value is not None else ""

    return (
        reveal(credentials.access_key),
        reveal(credentials.secret_key),
        reveal(credentials.session_token),
        reveal(credentials.token),
    )


class SignedKnowledgeResourceAuthority:
    """Fail-closed admission for signed, read-oriented platform resources."""

    def __init__(
        self,
        connections: ResourceConnectionRepository,
        policy: ResourceAuthorityPolicy,
    ) -> None:
        self.connections = connections
        self.policy = policy

    def admit(
        self,
        config: ResourceConfig,
        *,
        expected_connection_revision: int | None = None,
    ) -> VerifiedResourceAuthority:
        return self.admit_runtime(
            config,
            expected_connection_revision=expected_connection_revision,
        ).authority

    def resolve_signed_identity(self, access_key: str, secret_key: str) -> tuple[str, str]:
        """Resolve the host credential owner for an explicit connection declaration."""

        identity = self._verify_identity(access_key, secret_key)
        return identity.tenant_ref, identity.principal_ref

    def admit_runtime(
        self,
        config: ResourceConfig,
        *,
        expected_connection_revision: int | None = None,
    ) -> AdmittedResourceAccess:
        """Return only credentials proven inside this exact admission transaction."""

        resource = config.binding.resource
        field = "spec.bindings.plugins.config.binding"
        before = self.connections.get(config.binding.connection_ref)
        if (
            expected_connection_revision is not None
            and before.revision != expected_connection_revision
        ):
            raise StudioError(
                "RESOURCE_CONNECTION_CHANGED", "资源连接已变更，请刷新后重试", status_code=409
            )
        target = before.target
        if target.auth_mode != "signed":
            raise StudioError(
                "RESOURCE_AUTHORITY_UNSUPPORTED",
                "当前可信准入首片仅支持签名子账号连接",
                status_code=422,
                field=field + ".connectionRef",
            )
        if target.endpoint not in self.policy.allowed_data_endpoints or (
            resource.region not in self.policy.allowed_regions
        ):
            raise StudioError(
                "RESOURCE_AUTHORITY_TARGET_FORBIDDEN",
                "资源数据面地址或区域未被当前宿主批准",
                status_code=403,
                field=field + ".resource",
            )

        credentials = self.connections.resolve_credentials(target)
        values = _credential_values(credentials)
        if not values[0] or not values[1] or values[2] or values[3]:
            raise StudioError(
                "RESOURCE_AUTHORITY_UNSUPPORTED",
                "当前可信准入首片需要独立的 AK/SK 签名凭证",
                status_code=422,
            )
        identity = self._verify_identity(values[0], values[1])
        if (
            identity.tenant_ref != target.tenant_ref
            or identity.principal_ref != target.principal_ref
        ):
            raise StudioError(
                "RESOURCE_IDENTITY_MISMATCH",
                "连接声明的租户或主体与当前签名凭证不一致",
                status_code=403,
                field=field + ".connectionRef",
            )
        if resource.kind == "knowledge-base":
            request_id = self._prove_knowledge_read(
                config, target.endpoint, values[0], values[1]
            )
        elif resource.kind == "memory-instance":
            request_id = self._prove_memory_read(
                config, target.endpoint, values[0], values[1]
            )
        else:
            request_id = self._prove_skill_read(
                config, target.endpoint, values[0], values[1]
            )

        after = self.connections.get(config.binding.connection_ref)
        after_values = _credential_values(self.connections.resolve_credentials(after.target))
        if after != before or after_values != values:
            raise StudioError(
                "RESOURCE_CONNECTION_CHANGED", "资源连接或凭证在准入期间发生变更", status_code=409
            )
        now = datetime.now(timezone.utc)
        return AdmittedResourceAccess(
            authority=VerifiedResourceAuthority(
                connection_ref=target.connection_ref,
                connection_revision=before.revision,
                tenant_ref=identity.tenant_ref,
                resource_principal_ref=identity.principal_ref,
                resource=resource,
                allowed_operations=resource_allowed_operations(config),
                issuer_endpoint=self.policy.iam_endpoint,
                issuer_region=self.policy.iam_region,
                data_endpoint=target.endpoint,
                observed_at=now,
                expires_at=now + timedelta(seconds=self.policy.grant_ttl_seconds),
                request_id=request_id,
            ),
            credentials=credentials,
        )
    def _verify_identity(self, access_key: str, secret_key: str) -> _VerifiedIamIdentity:
        try:
            from ksyun.client.iam.v20151101.client import (  # type: ignore[import-untyped]
                IamClient,
            )
            from ksyun.client.iam.v20151101.models import (  # type: ignore[import-untyped]
                GetUserRequest,
                ListAllUserAccessKeysRequest,
            )
            from ksyun.common.credential import Credential  # type: ignore[import-untyped]
            from ksyun.common.profile.client_profile import (  # type: ignore[import-untyped]
                ClientProfile,
            )
            from ksyun.common.profile.http_profile import (  # type: ignore[import-untyped]
                HttpProfile,
            )

            endpoint = urlsplit(self.policy.iam_endpoint)
            profile = ClientProfile()
            profile.httpProfile = HttpProfile(
                protocol=endpoint.scheme,
                endpoint=endpoint.netloc,
                # The hardened transport owns the configured base path.
                path="/",
                reqMethod="POST",
                reqTimeout=self.policy.timeout_seconds,
            )
            client = IamClient(
                Credential(access_key, secret_key), self.policy.iam_region, profile
            )
            client.request = _HardenedSdkTransport(
                self.policy.iam_endpoint, timeout=self.policy.timeout_seconds
            )
            listed = _response_object(client.ListAllUserAccessKeys(ListAllUserAccessKeysRequest()))
            identity = self._identity_from_iam_listing(listed, access_key)
            request = GetUserRequest()
            request.UserName = identity
            user_response = _response_object(client.GetUser(request))
            return self._identity_from_iam_user(user_response, identity)
        except StudioError:
            raise
        except Exception as error:
            raise StudioError(
                "RESOURCE_IDENTITY_UNVERIFIED",
                "无法使用当前签名凭证验证平台子账号身份",
                status_code=403,
            ) from error

    @staticmethod
    def _identity_from_iam_listing(response: dict[str, Any], access_key: str) -> str:
        truncated = response.get("IsTruncated", False)
        if type(truncated) is not bool or truncated or response.get("NextMarker"):
            raise ValueError("incomplete access-key listing")
        has_primary = "AccessKeyList" in response
        has_alias = "AccessKeys" in response
        if has_primary == has_alias:
            raise ValueError("ambiguous access-key listing")
        entries = response["AccessKeyList" if has_primary else "AccessKeys"]
        if not isinstance(entries, list):
            raise ValueError("invalid access-key listing")
        matches = []
        for entry in entries:
            if not isinstance(entry, dict):
                raise ValueError("invalid access-key entry")
            has_key = "AccessKey" in entry
            has_alias_key = "AccessKeyId" in entry
            if has_key == has_alias_key:
                raise ValueError("ambiguous access-key entry")
            candidate = entry["AccessKey" if has_key else "AccessKeyId"]
            user_name = entry.get("UserName")
            if (
                not isinstance(candidate, str)
                or not candidate
                or candidate != candidate.strip()
                or not isinstance(user_name, str)
                or not user_name
                or user_name != user_name.strip()
            ):
                raise ValueError("invalid access-key identity")
            if candidate == access_key:
                matches.append(user_name)
        if len(matches) != 1:
            raise ValueError("signed access key has no unique sub-account")
        return matches[0]

    @staticmethod
    def _identity_from_iam_user(
        response: dict[str, Any], expected_user_name: str
    ) -> _VerifiedIamIdentity:
        result = response.get("GetUserResult")
        user = result.get("User") if isinstance(result, dict) else None
        if not isinstance(user, dict):
            raise ValueError("invalid IAM user response")
        user_id = user.get("UserId")
        user_name = user.get("UserName", expected_user_name)
        krn = user.get("Krn")
        if not all(
            isinstance(value, str) and value and value == value.strip()
            for value in (user_id, user_name, krn)
        ):
            raise ValueError("incomplete IAM user identity")
        match = _IAM_KRN.fullmatch(krn)
        if not match or user_name != expected_user_name or match.group(2) != user_name:
            raise ValueError("IAM user identity is inconsistent")
        return _VerifiedIamIdentity(tenant_ref=match.group(1), principal_ref=user_id)

    def _prove_knowledge_read(
        self,
        config: ResourceConfig,
        data_endpoint: str,
        access_key: str,
        secret_key: str,
    ) -> str:
        endpoint = urlsplit(data_endpoint)
        client = KnowledgeBaseClient(
            dataset_id=config.binding.resource.id,
            region=config.binding.resource.region,
            endpoint=endpoint.netloc,
            scheme=endpoint.scheme,
            access_key=access_key,
            secret_key=secret_key,
            top_k=1,
        )
        try:
            upstream = client._get_client()
            upstream.request = _HardenedSdkTransport(
                data_endpoint, timeout=self.policy.timeout_seconds
            )
            client.search("ksadk-resource-authority-probe", top_k=1)
            if client.last_http_status != 200 or client.last_error:
                raise ValueError("knowledge access was not proven")
            return client.last_request_id
        except Exception as error:
            raise StudioError(
                "RESOURCE_OPERATION_UNVERIFIED",
                "当前签名主体未通过指定知识库的只读检索校验",
                status_code=403,
            ) from error

    def _prove_memory_read(
        self,
        config: ResourceConfig,
        data_endpoint: str,
        access_key: str,
        secret_key: str,
    ) -> str:
        endpoint = urlsplit(data_endpoint)
        backend = SdkLTMBackend(
            index="resource-authority",
            memory_collection_id=config.binding.resource.id,
            namespace=config.binding.resource.id,
            agent_id="resource-authority",
            region=config.binding.resource.region,
            endpoint=endpoint.netloc,
            scheme=endpoint.scheme,
            access_key=access_key,
            secret_key=secret_key,
        )
        try:
            upstream = backend._get_client()
            upstream.request = _HardenedSdkTransport(
                data_endpoint, timeout=self.policy.timeout_seconds
            )
            backend.search_memory(
                "ksadk-resource-authority",
                "ksadk-resource-authority-probe",
                top_k=1,
            )
            if backend.last_http_status != 200 or backend.last_error:
                raise ValueError("memory access was not proven")
            return "memory-read-proven"
        except Exception as error:
            raise StudioError(
                "RESOURCE_OPERATION_UNVERIFIED",
                "当前签名主体未通过指定记忆库的只读召回校验",
                status_code=403,
            ) from error

    def _prove_skill_read(
        self,
        config: ResourceConfig,
        data_endpoint: str,
        access_key: str,
        secret_key: str,
    ) -> str:
        client = SkillServiceClient(
            base_url=data_endpoint,
            access_key=access_key,
            secret_key=secret_key,
            region=config.binding.resource.region,
            allow_env_fallback=False,
            timeout=self.policy.timeout_seconds,
        )
        try:
            listing = client.list_skills_by_space_id(
                config.binding.resource.id, max_pages=1
            )
            if listing.space_id != config.binding.resource.id:
                raise ValueError("skill space access was not proven")
            return "skill-space-read-proven"
        except Exception as error:
            raise StudioError(
                "RESOURCE_OPERATION_UNVERIFIED",
                "当前签名主体未通过指定 Skill Space 的只读目录校验",
                status_code=403,
            ) from error
        finally:
            client.close()


def resource_allowed_operations(config: ResourceConfig) -> tuple[ResourceOperation, ...]:
    """Return the maximum host capability set for one admitted resource kind.

    Activation policy narrows this set before issuing leases. Memory writes are
    still fail-closed unless that activation also owns a write authorizer.
    """

    kind = config.binding.resource.kind
    if kind == "knowledge-base":
        return ("search_knowledge_base",)
    if kind == "memory-instance":
        return ("load_memory", "save_memory", "memory_status")
    return ("list_skills", "search_skills", "load_skill", "read_skill_resource")


def resource_authority_policy_from_environment() -> ResourceAuthorityPolicy | None:
    """Build a trusted policy from operator-owned local Studio environment."""

    access_key = os.environ.get("KSYUN_ACCESS_KEY", "").strip()
    secret_key = os.environ.get("KSYUN_SECRET_KEY", "").strip()
    endpoint = os.environ.get("AGENTENGINE_SERVER_URL", "").strip()
    if not access_key or not secret_key or not endpoint:
        return None
    logical_region = (
        os.environ.get("AGENTENGINE_REGION")
        or os.environ.get("KSYUN_REGION")
        or "cn-beijing-6"
    ).strip()
    region = (
        os.environ.get("AGENTENGINE_PRE_CONTROL_REGION", "cn-beijing-6").strip()
        if logical_region.lower() == "pre-online"
        else logical_region
    )
    iam_endpoint = os.environ.get("KSADK_RESOURCE_IAM_ENDPOINT", "").strip()
    if not iam_endpoint:
        iam_endpoint = (
            "http://iam.inner.api.ksyun.com"
            if ".inner.api.ksyun.com" in endpoint
            else "https://iam.api.ksyun.com"
        )
    return ResourceAuthorityPolicy(
        iam_endpoint=iam_endpoint,
        iam_region=region,
        allowed_data_endpoints=(endpoint,),
        allowed_regions=(region,),
        allow_ksyun_internal_http=True,
    )

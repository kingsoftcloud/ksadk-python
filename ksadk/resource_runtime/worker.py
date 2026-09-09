"""Private resource worker entry point. Initialization arrives on a trusted pipe."""

from __future__ import annotations

import logging
import sys
import time
from dataclasses import asdict
from pathlib import Path
from typing import ClassVar
from urllib.parse import urlsplit

import httpx
from pydantic import ConfigDict, Field, SecretStr, model_validator
from requests.exceptions import RequestException

from ksadk.knowledge_base.client import KnowledgeBaseClient
from ksadk.memory.adk.backends.sdk_ltm_backend import SdkLTMBackend
from ksadk.memory.models import MemorySearchRequest
from ksadk.plugins.contracts import PluginContractModel
from ksadk.resource_runtime.build_artifacts import ResourceBuildReference
from ksadk.resource_runtime.contracts import (
    Identifier,
    InvocationIdentity,
    ResourceConfig,
    validate_resource_bindings,
)
from ksadk.resource_runtime.discovery_receipts import DiscoverySkillReceipt, discovery_scope_key
from ksadk.resource_runtime.discovery_skills import DiscoverySkillService
from ksadk.resource_runtime.errors import ResourceUpstreamAuthorizationError
from ksadk.resource_runtime.ipc import ResourceRequest, read_frame, write_frame
from ksadk.resource_runtime.knowledge import BoundKnowledgeService
from ksadk.resource_runtime.leases import ResourceScope
from ksadk.resource_runtime.memory_provider import AicpMemoryProvider, MemoryQuery
from ksadk.resource_runtime.skill_runtime import create_bound_skill_runtime
from ksadk.resource_runtime.skills import PinnedSkillService
from ksadk.resource_runtime.snapshots import ConnectionTarget, MemoryRecallPolicy, ResourceSnapshot
from ksadk.skills.package_store import SkillPackageError
from ksadk.skills.service_client import SkillServiceClient


class ExplicitWorkerBinding(PluginContractModel):
    resource_kind: ClassVar[str] = ""
    model_config = ConfigDict(hide_input_in_errors=True)
    config: ResourceConfig
    endpoint: str
    access_key: SecretStr = Field(min_length=1)
    secret_key: SecretStr = Field(min_length=1)
    session_token: SecretStr = SecretStr("")

    @model_validator(mode="after")
    def explicit_connection(self) -> ExplicitWorkerBinding:
        url = urlsplit(self.endpoint)
        if (
            url.scheme not in {"http", "https"}
            or not url.hostname
            or url.username is not None
            or url.password is not None
            or url.query
            or url.fragment
            or url.path not in {"", "/"}
            or any(char.isspace() for char in self.endpoint)
        ):
            raise ValueError("Worker requires an explicit service endpoint")
        if self.config.binding.resource.kind != self.resource_kind:
            raise ValueError("Worker binding resource kind does not match service")
        return self


class KnowledgeWorkerBinding(ExplicitWorkerBinding):
    resource_kind: ClassVar[str] = "knowledge-base"

    def create_service(self) -> BoundKnowledgeService:
        endpoint = urlsplit(self.endpoint)
        client = KnowledgeBaseClient(
            dataset_id=self.config.binding.resource.id,
            region=self.config.binding.resource.region,
            endpoint=endpoint.netloc,
            scheme=endpoint.scheme,
            access_key=self.access_key.get_secret_value(),
            secret_key=self.secret_key.get_secret_value(),
            session_token=self.session_token.get_secret_value(),
        )
        return BoundKnowledgeService(self.config, client)


class MemoryWorkerBinding(ExplicitWorkerBinding):
    resource_kind: ClassVar[str] = "memory-instance"

    def create_provider(self, identity: InvocationIdentity) -> AicpMemoryProvider:
        endpoint = urlsplit(self.endpoint)
        backend = SdkLTMBackend(
            index="bound-resource",
            memory_collection_id=self.config.binding.resource.id,
            namespace=self.config.binding.resource.id,
            agent_id=identity.agent_id,
            region=self.config.binding.resource.region,
            endpoint=endpoint.netloc,
            scheme=endpoint.scheme,
            access_key=self.access_key.get_secret_value(),
            secret_key=self.secret_key.get_secret_value(),
            session_token=self.session_token.get_secret_value(),
        )
        return AicpMemoryProvider(self.config, identity, backend)


class DiscoverySkillWorkerBinding(ExplicitWorkerBinding):
    resource_kind: ClassVar[str] = "skill-space"
    cache_directory: Path
    selection_run_ref: Identifier
    restored_selections: tuple[DiscoverySkillReceipt, ...] = Field(default=(), max_length=32)
    execution_connection: ConnectionTarget | None = None
    execution_api_key: SecretStr | None = Field(default=None, repr=False)
    artifact_directory: Path | None = None

    @model_validator(mode="after")
    def discovery_only(self):
        if (
            self.config.selection_mode != "discovery"
            or self.config.include_public
            or self.session_token.get_secret_value()
            or not self.cache_directory.is_absolute()
        ):
            raise ValueError("Discovery Worker requires private-space signed configuration")
        execution = (self.execution_connection, self.execution_api_key, self.artifact_directory)
        if any(value is not None for value in execution) and (
            any(value is None for value in execution)
            or not self.execution_api_key.get_secret_value()
            or not self.artifact_directory.is_absolute()
        ):
            raise ValueError(
                "Discovery execution requires complete explicit target and artifact root"
            )
        return self

    def create_provider(
        self, scope: ResourceScope, *, execution_target=None,
    ) -> DiscoverySkillService:
        namespace = discovery_scope_key(scope, self.selection_run_ref)
        client = SkillServiceClient(
            base_url=self.endpoint,
            access_key=self.access_key.get_secret_value(),
            secret_key=self.secret_key.get_secret_value(),
            region=self.config.binding.resource.region,
            allow_env_fallback=False,
            timeout=15,
        )
        return DiscoverySkillService(
            self.config,
            client,
            cache_directory=self.cache_directory,
            namespace=namespace,
            restored_selections=self.restored_selections,
            execution_target=execution_target,
        )


def read_memory(
    provider: AicpMemoryProvider,
    arguments: dict,
    policy: MemoryRecallPolicy | None = None,
) -> dict:
    query = MemoryQuery.model_validate(arguments)
    policy = MemoryRecallPolicy.model_validate((policy or MemoryRecallPolicy()).model_dump())
    result = provider.search(
        MemorySearchRequest(
            query=query.query,
            scopes=[("user", provider.partition)],
            top_k=policy.top_k,
            max_tokens=policy.max_tokens,
            min_score=policy.min_score,
            memory_types=[],
        )
    )
    return asdict(result)


class PinnedSkillWorkerBinding(PluginContractModel):
    model_config = ConfigDict(hide_input_in_errors=True)
    config: ResourceConfig
    build_directory: Path
    build_reference: ResourceBuildReference
    cache_directory: Path
    execution_connection: ConnectionTarget | None = None
    execution_api_key: SecretStr | None = Field(default=None, repr=False)
    artifact_directory: Path | None = None

    @model_validator(mode="after")
    def pinned_only(self):
        if (
            self.config.binding.resource.kind != "skill-space"
            or self.config.selection_mode == "discovery"
        ):
            raise ValueError("Pinned Skill Worker requires pinned selection")
        if not self.build_directory.is_absolute() or not self.cache_directory.is_absolute():
            raise ValueError("Worker delivery paths must be absolute host paths")
        execution_fields = (
            self.execution_connection,
            self.execution_api_key,
            self.artifact_directory,
        )
        if any(item is not None for item in execution_fields):
            if any(item is None for item in execution_fields):
                raise ValueError(
                    "Skill execution requires connection, credential and artifact root"
                )
            if not self.execution_api_key.get_secret_value():
                raise ValueError("Skill execution credential cannot be empty")
            if not self.artifact_directory.is_absolute():
                raise ValueError("Skill artifact root must be absolute")
        return self

    def create_service(self) -> PinnedSkillService:
        return PinnedSkillService(
            self.build_directory,
            self.build_reference,
            binding_id=self.config.binding.id,
            cache_directory=self.cache_directory,
        )


class WorkerInitialization(PluginContractModel):
    model_config = ConfigDict(hide_input_in_errors=True)
    resource_snapshot: ResourceSnapshot
    bindings: tuple[
        KnowledgeWorkerBinding
        | MemoryWorkerBinding
        | PinnedSkillWorkerBinding
        | DiscoverySkillWorkerBinding,
        ...,
    ]
    scopes: tuple[ResourceScope, ...]

    @model_validator(mode="after")
    def single_activation(self) -> WorkerInitialization:
        if not self.bindings or not self.scopes:
            raise ValueError("Worker initialization cannot be empty")
        validate_resource_bindings(tuple(binding.config for binding in self.bindings))
        binding_ids = {binding.config.binding.id for binding in self.bindings}
        if binding_ids != {scope.binding_id for scope in self.scopes}:
            raise ValueError("Worker scopes must exactly cover bindings")
        if len(self.scopes) != len(binding_ids):
            raise ValueError("Duplicate worker scope")
        first = self.scopes[0]
        if binding_ids != {item.config.binding.id for item in self.resource_snapshot.bindings}:
            raise ValueError("Worker bindings must exactly match the Build resource snapshot")
        for scope in self.scopes:
            frozen = self.resource_snapshot.binding(scope.binding_id)
            binding = next(
                item for item in self.bindings if item.config.binding.id == scope.binding_id
            )
            if (
                scope.binding_snapshot_digest != self.resource_snapshot.digest
                or binding.config.digest != frozen.config.digest
            ):
                raise ValueError("Worker resource configuration does not match Build")
            if isinstance(binding, PinnedSkillWorkerBinding):
                if (
                    binding.build_reference.snapshot_digest != self.resource_snapshot.digest
                    or frozen.connection.tenant_ref != scope.identity.tenant_ref
                    or frozen.connection.principal_ref != scope.identity.resource_principal_ref
                ):
                    raise ValueError(
                        "Pinned Skill Worker identity or snapshot does not match Build"
                    )
            else:
                self.resource_snapshot.verify_connection(
                    scope.binding_id,
                    ConnectionTarget(
                        connection_ref=binding.config.binding.connection_ref,
                        tenant_ref=scope.identity.tenant_ref,
                        principal_ref=scope.identity.resource_principal_ref,
                        endpoint=binding.endpoint,
                        auth_mode="sts" if binding.session_token.get_secret_value() else "signed",
                    ),
                )
            if isinstance(binding, (PinnedSkillWorkerBinding, DiscoverySkillWorkerBinding)):
                if binding.execution_connection is not None and (
                    frozen.skill_execution is None
                    or binding.execution_connection != frozen.skill_execution.connection
                ):
                    raise ValueError("Skill execution connection does not match Build")
            if (
                scope.identity != first.identity
                or scope.activation_id != first.activation_id
                or scope.generation_id != first.generation_id
                or scope.profile_digest != first.profile_digest
                or scope.build_digest != first.build_digest
                or scope.binding_snapshot_digest != first.binding_snapshot_digest
            ):
                raise ValueError("Worker scopes cannot mix activations or Builds")
            supported = (
                {"load_memory", "save_memory", "memory_status", "update_memory", "delete_memory"}
                if isinstance(binding, MemoryWorkerBinding)
                else {"search_knowledge_base"}
            )
            if isinstance(binding, DiscoverySkillWorkerBinding):
                supported = {"list_skills", "search_skills", "load_skill", "read_skill_resource"}
                if binding.execution_connection is not None:
                    supported.update({"execute_skills", "read_skill_artifact"})
            if isinstance(binding, PinnedSkillWorkerBinding):
                supported = {
                    "list_skills",
                    "search_skills",
                    "load_skill",
                    "read_skill_resource",
                    "read_skill_artifact",
                }
                if binding.execution_connection is not None:
                    supported.add("execute_skills")
            if not set(scope.allowed_operations) <= supported:
                raise ValueError("Unsupported worker operation")
        return self

    def pipe_payload(self) -> dict:
        """Credential-bearing data for the private child stdin only; never persist."""
        payload = self.model_dump(by_alias=True, mode="json")
        for raw, binding in zip(payload["bindings"], self.bindings):
            if isinstance(binding, (PinnedSkillWorkerBinding, DiscoverySkillWorkerBinding)):
                if binding.execution_api_key is not None:
                    raw["executionApiKey"] = binding.execution_api_key.get_secret_value()
            if isinstance(binding, PinnedSkillWorkerBinding):
                continue
            raw["accessKey"] = binding.access_key.get_secret_value()
            raw["secretKey"] = binding.secret_key.get_secret_value()
            raw["sessionToken"] = binding.session_token.get_secret_value()
        return payload


def main() -> int:
    logging.disable(logging.CRITICAL)
    source, target = sys.stdin.buffer, sys.stdout.buffer
    try:
        initialization = WorkerInitialization.model_validate(read_frame(source))
        scopes = {scope.binding_id: scope for scope in initialization.scopes}
        services = {
            binding.config.binding.id: (
                binding.create_provider(
                    scopes[binding.config.binding.id],
                    execution_target=initialization.resource_snapshot.binding(
                        binding.config.binding.id
                    ).skill_execution,
                )
                if isinstance(binding, DiscoverySkillWorkerBinding)
                else binding.create_provider(scopes[binding.config.binding.id].identity)
                if isinstance(binding, MemoryWorkerBinding)
                else binding.create_service()
            )
            for binding in initialization.bindings
        }
        skill_runtimes = {}
        for binding in initialization.bindings:
            if isinstance(binding, (PinnedSkillWorkerBinding, DiscoverySkillWorkerBinding)) and (
                binding.execution_connection is not None
            ):
                root = binding.artifact_directory
                root.mkdir(mode=0o700, parents=True, exist_ok=True)
                if root.is_symlink() or root.stat().st_mode & 0o077:
                    raise ValueError("Skill artifact root must be private")
                skill_runtimes[binding.config.binding.id] = create_bound_skill_runtime(
                    initialization.resource_snapshot,
                    binding.config.binding.id,
                    current_connection=binding.execution_connection,
                    api_key=binding.execution_api_key,
                    artifact_directory=root.resolve(),
                )
                skill_runtimes[binding.config.binding.id].preflight()
        write_frame(target, {"status": "ready"})
    except Exception:
        write_frame(target, {"error": {"code": "RESOURCE_WORKER_INITIALIZATION_FAILED"}})
        return 1
    revoked_bindings: set[str] = set()
    while True:
        try:
            message = read_frame(source)
            if message is None:
                return 0
            if set(message) != {"request", "scope"}:
                raise ValueError("Invalid worker message")
            request = ResourceRequest.model_validate(message["request"])
            scope = ResourceScope.model_validate(message["scope"])
            if scopes.get(scope.binding_id) != scope:
                raise ValueError("Scope does not match worker initialization")
            if request.deadline / 1000 <= time.time():
                raise ValueError("Request deadline exceeded")
            if request.check_only or request.operation not in scope.allowed_operations:
                raise ValueError("Unsupported worker operation")
            if scope.binding_id in revoked_bindings:
                raise ResourceUpstreamAuthorizationError()
            service = services[scope.binding_id]
            if request.operation == "load_memory" and isinstance(service, AicpMemoryProvider):
                reply = read_memory(
                    service,
                    request.arguments,
                    initialization.resource_snapshot.binding(scope.binding_id).memory_recall,
                )
            elif request.operation == "save_memory" and isinstance(service, AicpMemoryProvider):
                reply = service.submit(request.arguments, operation_id=request.request_id)
            elif request.operation == "memory_status" and isinstance(service, AicpMemoryProvider):
                reply = service.extraction_status(request.arguments)
            elif request.operation in {"update_memory", "delete_memory"} and isinstance(
                service, AicpMemoryProvider
            ):
                reply = service.mutate(
                    request.operation,
                    request.arguments,
                    operation_id=request.request_id,
                )
            elif request.operation == "search_knowledge_base":
                reply = service.search(request.arguments).model_dump(by_alias=True, mode="json")
            elif isinstance(service, PinnedSkillService):
                if request.operation == "execute_skills":
                    try:
                        arguments = dict(request.arguments)
                        execution_kwargs = {}
                        if isinstance(service, DiscoverySkillService):
                            execution_kwargs["approved_selections"] = tuple(
                                DiscoverySkillReceipt.model_validate(item)
                                for item in arguments.pop("_hostSelections", [])
                            )
                        execution = service.execute(
                            arguments,
                            backend=skill_runtimes[scope.binding_id],
                            operation_id=request.request_id,
                            timeout=max(
                                1, min(900, int(request.deadline / 1000 - time.time()) - 2)
                            ),
                            **execution_kwargs,
                        )
                        # Only the broker consumes these private paths. stdout/stderr
                        # may contain secrets or host paths and never cross this boundary.
                        known_outcome = (
                            execution.exit_code is not None
                            and not execution.timed_out
                            and not execution.error_type
                        )
                        reply = {
                            "operationId": request.request_id,
                            "status": (
                                "succeeded"
                                if execution.ok
                                else "failed"
                                if known_outcome
                                else "unknown"
                            ),
                            "outputFiles": execution.output_files if known_outcome else [],
                        }
                    except Exception:
                        reply = {
                            "operationId": request.request_id,
                            "status": "unknown",
                            "outputFiles": [],
                        }
                    write_frame(target, {"requestId": request.request_id, "result": reply})
                    continue
                handlers = {
                    "list_skills": service.list_skills,
                    "search_skills": service.search_skills,
                    "load_skill": service.load_skill,
                    "read_skill_resource": service.read_resource,
                }
                try:
                    reply = handlers[request.operation](request.arguments)
                except (httpx.HTTPError, RequestException) as error:
                    response = getattr(error, "response", None)
                    if response is not None and response.status_code in {401, 403}:
                        raise ResourceUpstreamAuthorizationError() from None
                    reply = {"status": "failed", "errorCode": "SKILL_RESOURCE_UNAVAILABLE"}
                except (ValueError, OSError, SkillPackageError):
                    reply = {"status": "failed", "errorCode": "SKILL_RESOURCE_UNAVAILABLE"}
                if (
                    isinstance(service, DiscoverySkillService)
                    and request.operation in {"load_skill", "read_skill_resource"}
                    and reply.get("status") == "ok"
                ):
                    reply["_discoverySelection"] = DiscoverySkillReceipt.from_ref(
                        service._selected[reply["skillId"]]
                    ).model_dump(by_alias=True, mode="json")
            else:
                raise ValueError("Unsupported worker service")
            if reply.get("errorCode", reply.get("error_code")) == "RESOURCE_FORBIDDEN":
                revoked_bindings.add(scope.binding_id)
            write_frame(
                target,
                {
                    "requestId": request.request_id,
                    "result": reply,
                },
            )
        except ResourceUpstreamAuthorizationError:
            revoked_bindings.add(scope.binding_id)
            write_frame(target, {
                "requestId": request.request_id,
                "result": {"status": "unauthorized", "errorCode": "RESOURCE_FORBIDDEN"},
            })
        except Exception:
            # Malformed transport or a protocol violation closes this worker.
            # No exception text, credentials or request content enters stdout.
            write_frame(target, {"error": {"code": "RESOURCE_WORKER_REQUEST_FAILED"}})
            return 1


if __name__ == "__main__":
    raise SystemExit(main())

"""DeepSeek Harness Profile -> AgentProvider sidecar vertical.

DSH owns its Bundle/Profile ABI and executes all Cordis plugin code.  KsADK
only starts a trusted, fixed host command and consumes the host's typed
``agent.provider/v1`` descriptor over a bounded JSONL RPC protocol.  The
descriptor is projected into an internal :class:`PluginManifest` solely so the
existing PluginHost can keep ownership of admission, activation fencing, and
cleanup; DSH package authors do not need to publish a KsADK manifest.

The sidecar is language-neutral: any executable that implements the frozen
JSONL protocol can host the Cordis composition.  Python is not part of the ABI.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import time
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import ConfigDict, Field, field_validator

from ksadk.plugins.bridges.dsh import DshProfileProjection
from ksadk.plugins.bundle import ResolvedPluginBundle
from ksadk.plugins.contracts import PluginContractModel, PluginManifest
from ksadk.plugins.host import PluginExecutionContext, PluginHostError

DSH_AGENT_PROVIDER_HOST_PROTOCOL = "ksadk.dsh-agent-provider-host/v1"
DSH_AGENT_PROVIDER_HOST_METHODS = frozenset(
    {
        "handshake",
        "describe",
        "preflight",
        "activate",
        "inventory",
        "execute",
        "cancel",
        "health",
        "drain",
        "dispose",
    }
)
DSH_HOST_USER_PERMISSION = "process:host-user"

_MAX_LINE_BYTES = 1024 * 1024
_ID = re.compile(r"^[a-z0-9]+(?:[._-][a-z0-9]+)*$")
_SEMVER = re.compile(r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_PACKAGE = re.compile(r"^(?:@[A-Za-z0-9._-]+/)?[A-Za-z0-9._-]+$")
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SECRET_ENV = re.compile(
    r"(?:^|_)(?:API_?KEY|ACCESS_?KEY|AUTH|BEARER|CREDENTIAL|PASSWORD|"
    r"PRIVATE_?KEY|SECRET|TOKEN)(?:_|$)",
    re.IGNORECASE,
)
_DIAGNOSTIC_SECRET = re.compile(
    r"(?ix)(?:\bsk-[a-z0-9_-]{12,}\b|\bBearer\s+[a-z0-9._~+/=-]{8,}|"
    r"\b(?:api[_-]?key|access[_-]?token|auth[_-]?token|password|secret)"
    r"\s*(?:=|:)\s*[^\s,;]+)"
)
_SAFE_INHERITED_ENV = frozenset(
    {"LANG", "LC_ALL", "PATH", "SYSTEMROOT", "TEMP", "TMP", "TMPDIR", "TZ", "WINDIR"}
)
_BLOCKED_ENV = frozenset({"PYTHONHOME", "PYTHONPATH", "LD_PRELOAD"})


@dataclass(frozen=True)
class DshCircuitSnapshot:
    """Observable host health state; it contains no command or credential data."""

    state: Literal["closed", "open", "half-open"]
    consecutive_failures: int
    retry_after_seconds: float


class DshCircuitBreaker:
    """Single-probe circuit breaker for DSH host transport and protocol faults.

    It deliberately does not retry RPC calls.  An ``execute`` frame can create
    external side effects, so replay remains the responsibility of the
    caller's idempotent run/session protocol.
    """

    def __init__(
        self,
        *,
        failure_threshold: int = 3,
        recovery_timeout: float = 5.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if failure_threshold < 1:
            raise ValueError("failure_threshold must be positive")
        if recovery_timeout < 0:
            raise ValueError("recovery_timeout cannot be negative")
        self._failure_threshold = failure_threshold
        self._recovery_timeout = recovery_timeout
        self._clock = clock
        self._failures = 0
        self._opened_at: float | None = None
        self._half_open_claimed = False

    def acquire(self) -> bool:
        if self._opened_at is None:
            return True
        if self._clock() - self._opened_at < self._recovery_timeout:
            return False
        if self._half_open_claimed:
            return False
        self._half_open_claimed = True
        return True

    def succeed(self) -> None:
        self._failures = 0
        self._opened_at = None
        self._half_open_claimed = False

    def fail(self) -> None:
        self._failures += 1
        self._half_open_claimed = False
        if self._failures >= self._failure_threshold:
            self._opened_at = self._clock()

    def snapshot(self) -> DshCircuitSnapshot:
        if self._opened_at is None:
            return DshCircuitSnapshot("closed", self._failures, 0.0)
        retry_after = max(0.0, self._recovery_timeout - (self._clock() - self._opened_at))
        return DshCircuitSnapshot(
            "half-open" if retry_after == 0 else "open",
            self._failures,
            retry_after,
        )


class _DshProviderModel(PluginContractModel):
    model_config = ConfigDict(
        alias_generator=lambda value: (
            value.split("_")[0] + "".join(part.capitalize() for part in value.split("_")[1:])
        ),
        populate_by_name=True,
        extra="forbid",
        frozen=True,
    )


class DshAgentProviderDescriptor(_DshProviderModel):
    """One AgentProvider contribution exposed by the composed DSH host."""

    descriptor_format: Literal["dsh.agent-provider-descriptor/v1"] = (
        "dsh.agent-provider-descriptor/v1"
    )
    ecosystem: Literal["dsh"] = "dsh"
    provider_id: str = Field(min_length=3, max_length=128)
    provider_version: str
    display_name: str = Field(min_length=1, max_length=256)
    plugin_name: str = Field(min_length=1, max_length=256)
    profile: str = Field(min_length=1, max_length=64)
    profile_digest: str
    definition: Literal["agent.provider/v1"] = "agent.provider/v1"
    slot: Literal["agent.execution"] = "agent.execution"
    runtime_protocols: tuple[str, ...] = ()

    @field_validator("provider_id")
    @classmethod
    def validate_provider_id(cls, value: str) -> str:
        if not _ID.fullmatch(value):
            raise ValueError("providerId must be a lowercase qualified id")
        return value

    @field_validator("provider_version")
    @classmethod
    def validate_provider_version(cls, value: str) -> str:
        if not _SEMVER.fullmatch(value):
            raise ValueError("providerVersion must use exact semantic versioning")
        return value

    @field_validator("plugin_name")
    @classmethod
    def validate_plugin_name(cls, value: str) -> str:
        if not _PACKAGE.fullmatch(value):
            raise ValueError("pluginName must be a valid DSH package name")
        return value

    @field_validator("profile_digest")
    @classmethod
    def validate_profile_digest(cls, value: str) -> str:
        if not _DIGEST.fullmatch(value):
            raise ValueError("profileDigest must be a lowercase sha256 digest")
        return value

    @field_validator("runtime_protocols")
    @classmethod
    def validate_runtime_protocols(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not item or len(item) > 128 for item in value):
            raise ValueError("runtimeProtocols entries must be non-empty and bounded")
        if len(value) != len(set(value)):
            raise ValueError("runtimeProtocols entries must be unique")
        return value

    @property
    def descriptor_digest(self) -> str:
        payload = json.dumps(
            self.model_dump(by_alias=True, exclude_none=True, mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return f"sha256:{hashlib.sha256(payload).hexdigest()}"


class DshAgentProviderPreflight(_DshProviderModel):
    ready: bool
    descriptor_digest: str
    profile_digest: str

    @field_validator("descriptor_digest", "profile_digest")
    @classmethod
    def validate_digest(cls, value: str) -> str:
        if not _DIGEST.fullmatch(value):
            raise ValueError("preflight digests must be lowercase sha256 values")
        return value


class DshAgentProviderInventory(_DshProviderModel):
    provider_id: str = Field(min_length=3, max_length=128)
    provider_version: str
    profile: str = Field(min_length=1, max_length=64)
    profile_digest: str
    descriptor_digest: str
    state: Literal["ready", "draining", "disposed", "failed"]
    activation_count: int = Field(ge=0)

    @field_validator("profile_digest", "descriptor_digest")
    @classmethod
    def validate_digest(cls, value: str) -> str:
        if not _DIGEST.fullmatch(value):
            raise ValueError("inventory digests must be lowercase sha256 values")
        return value


@dataclass(frozen=True)
class DshAgentProviderRegistration:
    """Selector-safe registration emitted only after successful preflight."""

    descriptor: DshAgentProviderDescriptor
    preflight: DshAgentProviderPreflight
    manifest: PluginManifest


class DshAgentProviderHost:
    """Supervise one fixed DSH provider host for an immutable Profile projection."""

    def __init__(
        self,
        command: Sequence[str],
        *,
        projection: DshProfileProjection,
        cwd: str | Path | None = None,
        environment: Mapping[str, str] | None = None,
        startup_timeout: float = 5.0,
        request_timeout: float = 30.0,
        shutdown_timeout: float = 2.0,
        circuit_failure_threshold: int = 3,
        circuit_recovery_timeout: float = 5.0,
    ) -> None:
        self._command = _validate_command(command)
        self._projection = projection
        self._cwd = Path(cwd).resolve() if cwd is not None else None
        self._environment = _minimal_environment(environment or {})
        self._startup_timeout = _positive_timeout(startup_timeout, "startup_timeout")
        self._request_timeout = _positive_timeout(request_timeout, "request_timeout")
        self._shutdown_timeout = _positive_timeout(shutdown_timeout, "shutdown_timeout")
        self._circuit = DshCircuitBreaker(
            failure_threshold=circuit_failure_threshold,
            recovery_timeout=circuit_recovery_timeout,
        )
        self._process: asyncio.subprocess.Process | None = None
        self._request_lock = asyncio.Lock()
        self._lifecycle_lock = asyncio.Lock()
        self._stderr_task: asyncio.Task[None] | None = None
        self._stderr_tail: deque[str] = deque(maxlen=64)
        self._next_request_id = 0
        self._descriptor: DshAgentProviderDescriptor | None = None
        self._preflight: DshAgentProviderPreflight | None = None
        self._disposed = False

    @property
    def pid(self) -> int | None:
        process = self._process
        return process.pid if process is not None and process.returncode is None else None

    @property
    def stderr_tail(self) -> tuple[str, ...]:
        return tuple(self._stderr_tail)

    @property
    def circuit_snapshot(self) -> DshCircuitSnapshot:
        return self._circuit.snapshot()

    async def start(self) -> None:
        async with self._lifecycle_lock:
            if self.pid is not None:
                return
            if self._disposed:
                raise PluginHostError("dsh_provider_host_disposed", "DSH provider host is disposed")
            self._require_circuit_probe()
            try:
                process = await asyncio.create_subprocess_exec(
                    *self._command,
                    cwd=str(self._cwd) if self._cwd is not None else None,
                    env=self._environment,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    limit=_MAX_LINE_BYTES + 1,
                    start_new_session=os.name == "posix",
                )
            except (OSError, ValueError) as error:
                self._circuit.fail()
                raise PluginHostError(
                    "dsh_provider_host_start_failed",
                    f"cannot start fixed DSH provider host: {type(error).__name__}",
                ) from error
            self._process = process
            self._stderr_task = asyncio.create_task(self._capture_stderr(process))
            try:
                handshake = await self._request(
                    "handshake",
                    {"profile": self._projection_payload()},
                    timeout=self._startup_timeout,
                    circuit_probe_acquired=True,
                )
                try:
                    self._validate_handshake(handshake)
                except PluginHostError:
                    self._circuit.fail()
                    raise
            except BaseException:
                await self._terminate()
                raise

    async def describe(self) -> DshAgentProviderDescriptor:
        await self.start()
        if self._descriptor is not None:
            return self._descriptor
        payload = await self._request("describe", {"profile": self._projection_payload()})
        try:
            descriptor = DshAgentProviderDescriptor.model_validate(payload)
        except Exception as error:  # noqa: BLE001 - external protocol boundary
            raise PluginHostError(
                "dsh_provider_descriptor_invalid",
                "DSH host returned an invalid provider descriptor",
            ) from error
        if descriptor.profile != self._projection.profile:
            raise PluginHostError(
                "dsh_provider_descriptor_mismatch", "DSH provider descriptor profile does not match"
            )
        if descriptor.profile_digest != self._projection.config_digest:
            raise PluginHostError(
                "dsh_provider_descriptor_mismatch",
                "DSH provider descriptor is not fenced to the selected Profile digest",
            )
        if descriptor.plugin_name not in self._projection.bundles:
            raise PluginHostError(
                "dsh_provider_descriptor_mismatch",
                "DSH provider descriptor does not belong to an active Profile bundle",
            )
        self._descriptor = descriptor
        return descriptor

    async def preflight(self) -> DshAgentProviderPreflight:
        descriptor = await self.describe()
        payload = await self._request(
            "preflight",
            {
                "profileDigest": self._projection.config_digest,
                "descriptorDigest": descriptor.descriptor_digest,
            },
        )
        try:
            result = DshAgentProviderPreflight.model_validate(payload)
        except Exception as error:  # noqa: BLE001 - external protocol boundary
            raise PluginHostError(
                "dsh_provider_preflight_invalid", "DSH host returned an invalid preflight result"
            ) from error
        if (
            result.descriptor_digest != descriptor.descriptor_digest
            or result.profile_digest != self._projection.config_digest
        ):
            raise PluginHostError(
                "dsh_provider_preflight_mismatch", "DSH preflight result crossed a Profile fence"
            )
        if not result.ready:
            raise PluginHostError(
                "dsh_provider_preflight_failed", "DSH AgentProvider is not ready for activation"
            )
        self._preflight = result
        return result

    async def registration(self) -> DshAgentProviderRegistration:
        """Return a selector-visible projection only after the provider is ready."""

        descriptor = await self.describe()
        preflight = await self.preflight()
        return DshAgentProviderRegistration(
            descriptor=descriptor,
            preflight=preflight,
            manifest=dsh_agent_provider_manifest(descriptor, preflight=preflight),
        )

    async def activate(
        self,
        bundle: ResolvedPluginBundle,
        capabilities: PluginExecutionContext,
    ) -> str:
        descriptor = await self.describe()
        await self.preflight()
        payload = await self._request(
            "activate",
            {
                "descriptorDigest": descriptor.descriptor_digest,
                "bundle": _bundle_payload(bundle),
                "capabilities": _capability_payload(capabilities),
            },
        )
        if not isinstance(payload, dict):
            raise PluginHostError(
                "dsh_provider_activation_invalid", "DSH host activation result must be an object"
            )
        activation_id = str(payload.get("activationId") or "").strip()
        if not activation_id or len(activation_id) > 256:
            raise PluginHostError(
                "dsh_provider_activation_invalid", "DSH host returned no valid activationId"
            )
        return activation_id

    async def inventory(self) -> DshAgentProviderInventory:
        descriptor = await self.describe()
        payload = await self._request(
            "inventory", {"descriptorDigest": descriptor.descriptor_digest}
        )
        try:
            result = DshAgentProviderInventory.model_validate(payload)
        except Exception as error:  # noqa: BLE001 - external protocol boundary
            raise PluginHostError(
                "dsh_provider_inventory_invalid", "DSH host returned invalid provider inventory"
            ) from error
        if (
            result.provider_id != descriptor.provider_id
            or result.provider_version != descriptor.provider_version
            or result.profile != descriptor.profile
            or result.profile_digest != descriptor.profile_digest
            or result.descriptor_digest != descriptor.descriptor_digest
        ):
            raise PluginHostError(
                "dsh_provider_inventory_mismatch",
                "DSH provider inventory crossed a descriptor fence",
            )
        return result

    async def health(self, *, activation_id: str | None = None) -> bool:
        params: dict[str, Any] = {"scope": "activation" if activation_id else "provider"}
        if activation_id:
            params["activationId"] = activation_id
        payload = await self._request("health", params)
        if not isinstance(payload, dict) or not isinstance(payload.get("healthy"), bool):
            raise PluginHostError(
                "dsh_provider_health_invalid", "DSH provider health must contain boolean healthy"
            )
        return bool(payload["healthy"])

    async def execute(self, activation_id: str, request: Any) -> Any:
        return await self._request(
            "execute", {"activationId": activation_id, "request": _json_value(request)}
        )

    async def cancel_activation(self, activation_id: str) -> None:
        await self._request("cancel", {"activationId": activation_id})

    async def drain(self, *, activation_id: str | None = None) -> None:
        params: dict[str, Any] = {"scope": "activation" if activation_id else "provider"}
        if activation_id:
            params["activationId"] = activation_id
        await self._request("drain", params)

    async def dispose_activation(self, activation_id: str) -> None:
        await self._request("dispose", {"scope": "activation", "activationId": activation_id})

    async def dispose(self) -> None:
        async with self._lifecycle_lock:
            if self._disposed:
                return
            self._disposed = True
            failure: Exception | None = None
            if self.pid is not None:
                try:
                    await self._request("dispose", {"scope": "host"})
                except Exception as error:  # cleanup must still terminate the sidecar
                    failure = error
            await self._terminate()
            if failure is not None:
                raise failure

    async def _request(
        self,
        method: str,
        params: Mapping[str, Any],
        *,
        timeout: float | None = None,
        circuit_probe_acquired: bool = False,
    ) -> Any:
        if method not in DSH_AGENT_PROVIDER_HOST_METHODS:
            raise PluginHostError(
                "dsh_provider_method_denied", f"DSH provider host method {method!r} is denied"
            )
        if not circuit_probe_acquired:
            self._require_circuit_probe()
        self._next_request_id += 1
        request_id = f"dsh-{self._next_request_id}"
        try:
            encoded = json.dumps(
                {"id": request_id, "method": method, "params": dict(params)},
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            ).encode("utf-8")
        except (TypeError, ValueError) as error:
            raise PluginHostError(
                "dsh_provider_request_invalid", "DSH provider request is not strict JSON"
            ) from error
        if len(encoded) > _MAX_LINE_BYTES:
            raise PluginHostError(
                "dsh_provider_request_too_large", "DSH provider request exceeds the protocol limit"
            )
        async with self._request_lock:
            process = self._process
            if (
                process is None
                or process.returncode is not None
                or process.stdin is None
                or process.stdout is None
            ):
                self._circuit.fail()
                raise PluginHostError(
                    "dsh_provider_host_unavailable", "DSH provider host is not running"
                )
            try:
                process.stdin.write(encoded + b"\n")
                await process.stdin.drain()
                line = await asyncio.wait_for(
                    process.stdout.readline(),
                    timeout=timeout if timeout is not None else self._request_timeout,
                )
            except asyncio.TimeoutError as error:
                await self._terminate()
                self._circuit.fail()
                raise PluginHostError(
                    "dsh_provider_request_timeout", f"DSH provider host timed out during {method}"
                ) from error
            except (BrokenPipeError, ConnectionError, OSError, ValueError) as error:
                await self._terminate()
                self._circuit.fail()
                raise PluginHostError(
                    "dsh_provider_transport_failed", f"DSH provider host failed during {method}"
                ) from error
            if not line or len(line) > _MAX_LINE_BYTES:
                await self._terminate()
                self._circuit.fail()
                raise PluginHostError(
                    "dsh_provider_protocol_invalid", "DSH provider host emitted no bounded response"
                )
            try:
                response = json.loads(line)
            except (UnicodeDecodeError, ValueError) as error:
                await self._terminate()
                self._circuit.fail()
                raise PluginHostError(
                    "dsh_provider_protocol_invalid", "DSH provider host emitted invalid JSONL"
                ) from error
            if not isinstance(response, dict) or response.get("id") != request_id:
                await self._terminate()
                self._circuit.fail()
                raise PluginHostError(
                    "dsh_provider_response_mismatch", "DSH provider response id does not match"
                )
            if response.get("error") is not None:
                raise PluginHostError(
                    "dsh_provider_remote_error", "DSH provider host rejected the request"
                )
            if "result" not in response:
                self._circuit.fail()
                raise PluginHostError(
                    "dsh_provider_protocol_invalid", "DSH provider response has no result"
                )
            self._circuit.succeed()
            return response["result"]

    def _require_circuit_probe(self) -> None:
        if self._circuit.acquire():
            return
        snapshot = self._circuit.snapshot()
        raise PluginHostError(
            "dsh_provider_circuit_open",
            "DSH provider host circuit is open; wait "
            f"{snapshot.retry_after_seconds:.1f}s before another probe",
        )

    def _validate_handshake(self, payload: Any) -> None:
        if not isinstance(payload, dict):
            raise PluginHostError(
                "dsh_provider_handshake_invalid", "DSH provider handshake must be an object"
            )
        methods = payload.get("methods")
        host_version = payload.get("hostVersion")
        if (
            payload.get("protocolVersion") != DSH_AGENT_PROVIDER_HOST_PROTOCOL
            or not isinstance(methods, list)
            or set(methods) != DSH_AGENT_PROVIDER_HOST_METHODS
            or len(methods) != len(DSH_AGENT_PROVIDER_HOST_METHODS)
            or not isinstance(host_version, str)
            or not _SEMVER.fullmatch(host_version)
        ):
            raise PluginHostError(
                "dsh_provider_handshake_invalid",
                "DSH provider host protocol, methods, or version is incompatible",
            )

    def _projection_payload(self) -> dict[str, Any]:
        return self._projection.model_dump(by_alias=True, mode="json")

    async def _capture_stderr(self, process: asyncio.subprocess.Process) -> None:
        if process.stderr is None:
            return
        while True:
            line = await process.stderr.readline()
            if not line:
                return
            diagnostic = line.decode("utf-8", errors="replace").rstrip()[:4096]
            self._stderr_tail.append(_DIAGNOSTIC_SECRET.sub("[REDACTED]", diagnostic))

    async def _terminate(self) -> None:
        process = self._process
        self._process = None
        if process is not None and process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=self._shutdown_timeout)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
        task = self._stderr_task
        self._stderr_task = None
        if task is not None and task is not asyncio.current_task():
            try:
                await asyncio.wait_for(task, timeout=self._shutdown_timeout)
            except asyncio.TimeoutError:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)


class DshAgentProviderRuntime:
    """Existing PluginHost AgentProvider SPI backed by one DSH host."""

    def __init__(self, host: DshAgentProviderHost) -> None:
        self._host = host
        self._ready = False
        self._disposed = False

    async def start(self) -> None:
        if self._disposed:
            raise PluginHostError("dsh_provider_disposed", "DSH AgentProvider is disposed")
        await self._host.preflight()
        self._ready = True

    async def health(self) -> bool:
        return (
            self._ready
            and not self._disposed
            and self._host.pid is not None
            and await self._host.health()
        )

    async def prepare(
        self,
        bundle: ResolvedPluginBundle,
        *,
        capabilities: PluginExecutionContext,
    ) -> "DshPreparedAgent":
        if not await self.health():
            raise PluginHostError("dsh_provider_unavailable", "DSH AgentProvider is not healthy")
        activation_id = await self._host.activate(bundle, capabilities)
        return DshPreparedAgent(self._host, activation_id)

    async def inventory(self) -> DshAgentProviderInventory:
        return await self._host.inventory()

    async def drain(self) -> None:
        if self._ready and not self._disposed and self._host.pid is not None:
            await self._host.drain()
        self._ready = False

    async def dispose(self) -> None:
        if self._disposed:
            return
        self._disposed = True
        self._ready = False
        await self._host.dispose()


class DshPreparedAgent:
    def __init__(self, host: DshAgentProviderHost, activation_id: str) -> None:
        self._host = host
        self._activation_id = activation_id
        self._started = False
        self._drained = False
        self._disposed = False

    async def start(self) -> None:
        if self._disposed:
            raise PluginHostError("dsh_activation_disposed", "DSH activation is disposed")
        self._started = True

    async def health(self) -> bool:
        return (
            self._started
            and not self._drained
            and not self._disposed
            and await self._host.health(activation_id=self._activation_id)
        )

    async def execute(self, request: Any) -> Any:
        if not await self.health():
            raise PluginHostError("dsh_activation_unavailable", "DSH activation is not healthy")
        return await self._host.execute(self._activation_id, request)

    async def cancel(self) -> None:
        """Request bounded cancellation without losing the disposable handle."""

        if self._disposed:
            return
        await self._host.cancel_activation(self._activation_id)

    async def drain(self) -> None:
        if self._disposed or self._drained:
            return
        await self._host.drain(activation_id=self._activation_id)
        self._drained = True

    async def dispose(self) -> None:
        if self._disposed:
            return
        self._disposed = True
        if self._host.pid is not None:
            await self._host.dispose_activation(self._activation_id)


class DshAgentProviderFactory:
    """Project one discovered DSH provider into the internal PluginFactory seam."""

    def __init__(
        self,
        host: DshAgentProviderHost,
        registration: DshAgentProviderRegistration,
    ) -> None:
        self._host = host
        self._registration = registration
        self.runtime: DshAgentProviderRuntime | None = None

    async def stage(
        self,
        manifest: PluginManifest,
        *,
        profile: Any,
        services: Mapping[str, Any],
    ) -> DshAgentProviderRuntime:
        del profile, services
        expected = dsh_agent_provider_manifest(
            self._registration.descriptor,
            preflight=self._registration.preflight,
        )
        if manifest != expected:
            raise PluginHostError(
                "dsh_provider_manifest_mismatch",
                "internal DSH provider projection does not match the discovered descriptor",
            )
        self.runtime = DshAgentProviderRuntime(self._host)
        return self.runtime


def dsh_agent_provider_manifest(
    descriptor: DshAgentProviderDescriptor,
    *,
    preflight: DshAgentProviderPreflight,
) -> PluginManifest:
    """Create an internal admission projection; this is not a DSH package format."""

    if (
        not preflight.ready
        or preflight.descriptor_digest != descriptor.descriptor_digest
        or preflight.profile_digest != descriptor.profile_digest
    ):
        raise PluginHostError(
            "dsh_provider_not_ready",
            "DSH AgentProvider cannot enter the selector before matching preflight",
        )

    return PluginManifest.model_validate(
        {
            "metadata": {
                "id": descriptor.provider_id,
                "version": descriptor.provider_version,
            },
            "spec": {
                "domain": "runtime-native",
                "runtime": "process",
                "entrypoint": "deepseek-harness:profile-agent-provider",
                "provides": [
                    {
                        "definition": descriptor.definition,
                        "slot": descriptor.slot,
                        "mode": "unique",
                    }
                ],
                "permissions": [DSH_HOST_USER_PERMISSION],
                "isolation": "sidecar",
                "compatibility": {
                    "kernelApi": ">=1,<2",
                    "runtimeProtocols": list(descriptor.runtime_protocols),
                },
                "healthContract": "plugin.health/v1",
                "provenance": {
                    "source": "runtime-native",
                    "digest": descriptor.descriptor_digest,
                },
            },
        }
    )


def _validate_command(command: Sequence[str]) -> tuple[str, ...]:
    if isinstance(command, (str, bytes)):
        raise PluginHostError(
            "dsh_provider_command_invalid", "DSH host command must be an argv sequence"
        )
    normalized = tuple(str(item) for item in command)
    if not normalized or any(not item or "\x00" in item for item in normalized):
        raise PluginHostError(
            "dsh_provider_command_invalid", "DSH host command contains an invalid argv item"
        )
    return normalized


def _minimal_environment(explicit: Mapping[str, str]) -> dict[str, str]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if key in _SAFE_INHERITED_ENV and not _SECRET_ENV.search(key)
    }
    for key, value in explicit.items():
        if (
            not _ENV_NAME.fullmatch(key)
            or key in _BLOCKED_ENV
            or key.startswith("DYLD_")
            or _SECRET_ENV.search(key)
        ):
            raise PluginHostError(
                "dsh_provider_environment_denied",
                f"DSH host environment key {key!r} is not allowed",
            )
        environment[key] = str(value)
    return environment


def _positive_timeout(value: float, field: str) -> float:
    normalized = float(value)
    if normalized <= 0:
        raise PluginHostError("dsh_provider_timeout_invalid", f"{field} must be positive")
    return normalized


def _bundle_payload(bundle: ResolvedPluginBundle) -> dict[str, Any]:
    return {
        "root": str(bundle.root),
        "manifest": bundle.manifest.model_dump(by_alias=True, exclude_none=True, mode="json"),
        "resolvedAgentSpec": _json_value(bundle.resolved_agent_spec),
        "composition": {
            "profileDigest": bundle.composition.profile_digest,
            "pluginLockDigest": bundle.composition.plugin_lock_digest,
        },
    }


def _capability_payload(context: PluginExecutionContext) -> dict[str, Any]:
    return {
        "profileDigest": context.profile_digest,
        "pluginLockDigest": context.plugin_lock_digest,
        "bindings": [
            {
                "pluginId": binding.plugin_id,
                "pluginVersion": binding.plugin_version,
                "definition": binding.definition,
                "slot": binding.slot,
            }
            for binding in context.bindings
        ],
    }


def _json_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        normalized: Any = {str(key): _json_value(child) for key, child in value.items()}
    elif isinstance(value, (list, tuple)):
        normalized = [_json_value(child) for child in value]
    elif value is None or isinstance(value, (str, int, float, bool)):
        normalized = value
    else:
        raise PluginHostError(
            "dsh_provider_request_invalid",
            f"DSH provider value {type(value).__name__} is not JSON compatible",
        )
    try:
        json.dumps(normalized, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as error:
        raise PluginHostError(
            "dsh_provider_request_invalid", "DSH provider value is not strict JSON"
        ) from error
    return normalized


__all__ = [
    "DSH_AGENT_PROVIDER_HOST_METHODS",
    "DSH_AGENT_PROVIDER_HOST_PROTOCOL",
    "DSH_HOST_USER_PERMISSION",
    "DshAgentProviderDescriptor",
    "DshAgentProviderFactory",
    "DshAgentProviderHost",
    "DshAgentProviderInventory",
    "DshAgentProviderPreflight",
    "DshAgentProviderRegistration",
    "DshAgentProviderRuntime",
    "DshCircuitBreaker",
    "DshCircuitSnapshot",
    "DshPreparedAgent",
    "dsh_agent_provider_manifest",
]

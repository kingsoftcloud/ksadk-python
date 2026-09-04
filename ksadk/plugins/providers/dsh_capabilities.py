"""Profile-level DSH tool capabilities exposed through a real MCP transport.

This module is deliberately orthogonal to ``dsh-agent-provider-host/v1``.  It
boots one complete, immutable DSH Profile with a wheel-owned Cordis plugin,
reads a one-shot readiness record, and returns a process-scoped authenticated
Streamable HTTP MCP lease.  Tool discovery and execution stay inside the real
``ctx.tools`` registry and its policy/cancellation pipeline.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import math
import os
import re
import secrets
import shutil
import signal
import struct
import tempfile
import time
from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, cast
from urllib.parse import urlsplit

import httpx
from pydantic import Field, field_validator, model_validator

from ksadk.plugins.bridges.dsh import DshProfileProjection, dsh_subprocess_environment
from ksadk.plugins.contracts import PluginContractModel
from ksadk.plugins.host import PluginHostError
from ksadk.plugins.providers.dsh import DshCircuitBreaker, DshCircuitSnapshot

DSH_CAPABILITY_HOST_PROTOCOL = "ksadk.dsh-capability-host/v1"
DSH_CAPABILITY_HOST_VERSION = "1.0.0"
DSH_CAPABILITY_BUNDLE_PACKAGE = "@kingsoftcloud/ksadk-dsh-capability-host"
DSH_CAPABILITY_DEFINITION = "mcp.connector/v1"
DSH_CAPABILITY_TRANSPORT = "streamable-http"
DSH_CAPABILITY_MCP_PROTOCOL = "2025-06-18"
DSH_CAPABILITY_MIN_NODE_VERSION = (22, 19, 0)

_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_SEMVER = re.compile(r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$")
_PROFILE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_TOOL_NAME = re.compile(r"^[A-Za-z0-9_.:-]+$")
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_NODE_VERSION = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?:[-+].*)?$")
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
_BLOCKED_ENV = frozenset(
    {
        "CORDIS_SHARED",
        "DSH_HOME",
        "KSADK_DSH_CAPABILITY_READY_FILE",
        "KSADK_DSH_CAPABILITY_READY_STDOUT",
        "KSADK_DSH_CAPABILITY_TOKEN",
        "KSADK_DSH_PROFILE",
        "KSADK_DSH_PROFILE_DIGEST",
        "KSADK_DSH_VERSION",
        "LD_PRELOAD",
        "NODE_OPTIONS",
        "PYTHONHOME",
        "PYTHONPATH",
    }
)
_MAX_READY_BYTES = 2 * 1024 * 1024
_MAX_CONFIG_BYTES = 8 * 1024 * 1024
_MAX_DIAGNOSTIC_CHARS = 4096
_NODE_MEMORY_LIMIT_MB = 512
_PROCESS_GROUP_ISOLATION_AVAILABLE = os.name == "posix"
_READY_PREFIX = "@@KSADK_DSH_CAPABILITY_READY@@"
_TOKEN_PREFIX = "@@KSADK_DSH_CAPABILITY_TOKEN@@"
_SCOPED_TOKEN_LIFETIME_SECONDS = 10 * 60


def _canonical_digest(value: Any) -> str:
    try:
        payload = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise ValueError("value must be strict JSON") from error
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def _canonical_json_value_bytes(value: Any) -> bytes:
    """Encode a JSON value identically in Python and the Node capability host.

    JSON text is not a cross-language canonical form: CPython and ECMAScript
    choose different spellings for valid binary64 values (for example
    ``1e-07`` versus ``1e-7``), and their native object-key ordering differs
    for some Unicode keys.  The inventory fence therefore uses a tiny tagged
    value encoding with UTF-8 byte ordering and IEEE-754 bits for numbers.
    It is not a new transport format; only the SHA-256 input uses these bytes.
    """

    if value is None:
        return b"n;"
    if isinstance(value, bool):
        return b"b1;" if value else b"b0;"
    if isinstance(value, (int, float)):
        try:
            number = float(value)
        except (OverflowError, ValueError) as error:
            raise ValueError("JSON numbers must fit finite IEEE-754 binary64") from error
        if not math.isfinite(number):
            raise ValueError("JSON numbers must fit finite IEEE-754 binary64")
        # JSON.stringify(-0) emits 0, so the digest also normalizes signed zero.
        if number == 0:
            number = 0.0
        return b"d" + struct.pack(">d", number).hex().encode("ascii") + b";"
    if isinstance(value, str):
        try:
            encoded = value.encode("utf-8")
        except UnicodeEncodeError as error:
            raise ValueError("JSON strings must contain Unicode scalar values") from error
        return f"s{len(encoded)}:".encode("ascii") + encoded
    if isinstance(value, (list, tuple)):
        return f"a{len(value)}:".encode("ascii") + b"".join(
            _canonical_json_value_bytes(item) for item in value
        )
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise ValueError("JSON object keys must be strings")
        keys = sorted(value, key=lambda key: key.encode("utf-8"))
        return f"o{len(keys)}:".encode("ascii") + b"".join(
            _canonical_json_value_bytes(key) + _canonical_json_value_bytes(value[key])
            for key in keys
        )
    raise ValueError("tool schemas must contain only JSON values")


class DshCapabilityTool(PluginContractModel):
    """One immutable MCP projection of a DSH ``ToolSchema``."""

    name: str = Field(min_length=1, max_length=128)
    # Normalize the upstream DSH limit to Studio/OpenAI's portable tool limit.
    description: str = Field(max_length=1024)
    input_schema: dict[str, Any]

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        if not _TOOL_NAME.fullmatch(value):
            raise ValueError("tool name contains unsupported characters")
        return value

    @model_validator(mode="after")
    def validate_schema(self) -> "DshCapabilityTool":
        payload = self.model_dump(by_alias=True, mode="json")
        # This also rejects lone UTF-16 surrogates and non-finite/out-of-range
        # numbers before they can make descriptor hashing or API serialization
        # fail later in the lifecycle.
        _canonical_json_value_bytes(payload)
        encoded = json.dumps(
            payload,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(encoded) > _MAX_READY_BYTES:
            raise ValueError("tool schema exceeds the capability descriptor limit")
        return self


def _inventory_digest(tools: Sequence[DshCapabilityTool]) -> str:
    payload = [tool.model_dump(by_alias=True, mode="json") for tool in tools]
    return f"sha256:{hashlib.sha256(_canonical_json_value_bytes(payload)).hexdigest()}"


def _tool_sort_key(tool: DshCapabilityTool) -> bytes:
    return tool.name.encode("utf-8")


class DshProfileCapabilityReady(PluginContractModel):
    """One-shot record written by the Cordis bridge after its listener binds."""

    protocol_version: Literal["ksadk.dsh-capability-host/v1"]
    host_version: str
    dsh_version: str
    profile: str = Field(min_length=1, max_length=64)
    profile_digest: str
    definition: Literal["mcp.connector/v1"]
    transport: Literal["streamable-http"]
    endpoint: str
    inventory_digest: str
    tools: tuple[DshCapabilityTool, ...] = Field(max_length=2048)

    @field_validator("host_version", "dsh_version")
    @classmethod
    def validate_version(cls, value: str) -> str:
        if not _SEMVER.fullmatch(value):
            raise ValueError("host versions must use exact semantic versioning")
        return value

    @field_validator("profile")
    @classmethod
    def validate_profile(cls, value: str) -> str:
        if not _PROFILE.fullmatch(value):
            raise ValueError("profile name is invalid")
        return value

    @field_validator("profile_digest", "inventory_digest")
    @classmethod
    def validate_digest(cls, value: str) -> str:
        if not _DIGEST.fullmatch(value):
            raise ValueError("capability digest is invalid")
        return value

    @field_validator("endpoint")
    @classmethod
    def validate_endpoint(cls, value: str) -> str:
        parsed = urlsplit(value)
        if (
            parsed.scheme != "http"
            or parsed.hostname != "127.0.0.1"
            or parsed.port is None
            or parsed.port < 1
            or parsed.path != "/mcp"
            or parsed.query
            or parsed.fragment
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise ValueError("capability endpoint must be an unadorned loopback /mcp URL")
        return value

    @model_validator(mode="after")
    def validate_inventory(self) -> "DshProfileCapabilityReady":
        names = [tool.name for tool in self.tools]
        if list(self.tools) != sorted(self.tools, key=_tool_sort_key) or len(names) != len(
            set(names)
        ):
            raise ValueError("capability tools must be sorted and unique")
        if _inventory_digest(self.tools) != self.inventory_digest:
            raise ValueError("capability inventory digest does not match its tools")
        return self


class DshProfileCapabilityDescriptor(PluginContractModel):
    """Stable profile capability facts; excludes the ephemeral URL and token."""

    descriptor_format: Literal["dsh.profile-capability-descriptor/v1"] = (
        "dsh.profile-capability-descriptor/v1"
    )
    ecosystem: Literal["dsh"] = "dsh"
    bridge_package: Literal["@kingsoftcloud/ksadk-dsh-capability-host"] = (
        "@kingsoftcloud/ksadk-dsh-capability-host"
    )
    bridge_version: Literal["1.0.0"] = "1.0.0"
    dsh_version: str
    profile: str = Field(min_length=1, max_length=64)
    profile_digest: str
    definition: Literal["mcp.connector/v1"] = "mcp.connector/v1"
    transport: Literal["streamable-http"] = "streamable-http"
    mcp_protocol: Literal["2025-06-18"] = "2025-06-18"
    inventory_digest: str
    tools: tuple[DshCapabilityTool, ...] = Field(max_length=2048)

    @field_validator("dsh_version")
    @classmethod
    def validate_dsh_version(cls, value: str) -> str:
        if not _SEMVER.fullmatch(value):
            raise ValueError("dshVersion must use exact semantic versioning")
        return value

    @field_validator("profile")
    @classmethod
    def validate_descriptor_profile(cls, value: str) -> str:
        if not _PROFILE.fullmatch(value):
            raise ValueError("profile name is invalid")
        return value

    @field_validator("profile_digest", "inventory_digest")
    @classmethod
    def validate_descriptor_digest(cls, value: str) -> str:
        if not _DIGEST.fullmatch(value):
            raise ValueError("descriptor digest field is invalid")
        return value

    @model_validator(mode="after")
    def validate_descriptor_inventory(self) -> "DshProfileCapabilityDescriptor":
        names = [tool.name for tool in self.tools]
        if list(self.tools) != sorted(self.tools, key=_tool_sort_key) or len(names) != len(
            set(names)
        ):
            raise ValueError("descriptor tools must be sorted and unique")
        if _inventory_digest(self.tools) != self.inventory_digest:
            raise ValueError("descriptor inventory digest does not match its tools")
        return self

    @property
    def descriptor_digest(self) -> str:
        return _canonical_digest(self.model_dump(by_alias=True, mode="json"))


@dataclass(frozen=True)
class DshMcpConnectorLease:
    """Ephemeral connection material for one running ``mcp.connector/v1``."""

    endpoint: str = field(repr=False)
    profile: str
    profile_digest: str
    descriptor_digest: str
    _bearer_token: str = field(repr=False)
    definition: Literal["mcp.connector/v1"] = "mcp.connector/v1"
    transport: Literal["streamable-http"] = "streamable-http"
    protocol_version: Literal["2025-06-18"] = "2025-06-18"

    def headers(self) -> dict[str, str]:
        """Return fresh request headers without storing them in descriptors/logs."""

        return {"Authorization": f"Bearer {self._bearer_token}"}

    def bearer_token_for_runtime(self, tool_aliases: Mapping[str, str]) -> str:
        """Mint a generation-bound token limited to explicit tool names."""

        aliases = {
            str(alias).strip(): str(source).strip()
            for alias, source in tool_aliases.items()
        }
        if (
            not aliases
            or len(aliases) != len(tool_aliases)
            or len(aliases) > 32
            or any(
                not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", alias)
                or not _TOOL_NAME.fullmatch(source)
                for alias, source in aliases.items()
            )
        ):
            raise PluginHostError(
                "dsh_capability_scope_invalid",
                "DSH capability tokens require an explicit valid tool scope",
            )
        payload = json.dumps(
            {
                "v": 1,
                "profileDigest": self.profile_digest,
                "exp": int(time.time()) + _SCOPED_TOKEN_LIFETIME_SECONDS,
                "jti": secrets.token_urlsafe(16),
                "aliases": dict(sorted(aliases.items())),
            },
            allow_nan=False,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        encoded = base64.urlsafe_b64encode(payload).rstrip(b"=")
        signature = hmac.new(
            self._bearer_token.encode("utf-8"), encoded, hashlib.sha256
        ).digest()
        encoded_signature = base64.urlsafe_b64encode(signature).rstrip(b"=")
        token = f"ks1.{encoded.decode('ascii')}.{encoded_signature.decode('ascii')}"
        if len(token) > 12 * 1024:
            raise PluginHostError(
                "dsh_capability_scope_too_large",
                "DSH capability token scope exceeds the HTTP header budget",
            )
        return token


class DshProfileCapabilityInventory(PluginContractModel):
    ecosystem: Literal["dsh"] = "dsh"
    profile: str
    profile_digest: str
    descriptor_digest: str | None = None
    inventory_digest: str | None = None
    state: Literal["stopped", "starting", "ready", "failed", "disposed"]
    pid: int | None = Field(default=None, ge=1)
    tool_count: int = Field(ge=0)
    circuit_state: Literal["closed", "open", "half-open"]
    consecutive_failures: int = Field(ge=0)
    retry_after_seconds: float = Field(ge=0)


@dataclass(frozen=True)
class DshCapabilityBundle:
    package_name: str
    version: str
    root: Path
    entrypoint: Path
    patch: Path
    digest: str


def load_dsh_capability_bundle() -> DshCapabilityBundle:
    """Locate and validate the immutable bridge assets shipped in the wheel."""

    root = Path(__file__).with_name("bundles") / "ksadk-dsh-capability-host"
    manifest_path = root / "package.json"
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise PluginHostError(
            "dsh_capability_bundle_invalid", "DSH capability Bundle manifest is unavailable"
        ) from error
    dsh = payload.get("dsh") if isinstance(payload, dict) else None
    bundle = dsh.get("bundle") if isinstance(dsh, dict) else None
    patch_value = bundle.get("patch") if isinstance(bundle, dict) else None
    integrity = bundle.get("integrity") if isinstance(bundle, dict) else None
    if (
        not isinstance(payload, dict)
        or payload.get("name") != DSH_CAPABILITY_BUNDLE_PACKAGE
        or payload.get("version") != DSH_CAPABILITY_HOST_VERSION
        or payload.get("type") != "module"
        or patch_value != "./cordis.patch.yml"
        or not isinstance(integrity, str)
        or not _DIGEST.fullmatch(integrity)
    ):
        raise PluginHostError(
            "dsh_capability_bundle_invalid", "DSH capability Bundle identity is invalid"
        )
    entrypoint = (root / "index.mjs").resolve()
    patch = (root / str(patch_value)).resolve()
    resolved_root = root.resolve()
    if (
        not entrypoint.is_relative_to(resolved_root)
        or not patch.is_relative_to(resolved_root)
        or not entrypoint.is_file()
        or not patch.is_file()
    ):
        raise PluginHostError(
            "dsh_capability_bundle_invalid", "DSH capability Bundle assets are missing"
        )
    hasher = hashlib.sha256()
    for name, path in (("index.mjs", entrypoint), ("cordis.patch.yml", patch)):
        hasher.update(name.encode("utf-8"))
        hasher.update(b"\0")
        hasher.update(path.read_bytes())
        hasher.update(b"\0")
    digest = f"sha256:{hasher.hexdigest()}"
    if digest != integrity:
        raise PluginHostError(
            "dsh_capability_bundle_invalid",
            "DSH capability Bundle integrity does not match its assets",
        )
    return DshCapabilityBundle(
        package_name=DSH_CAPABILITY_BUNDLE_PACKAGE,
        version=DSH_CAPABILITY_HOST_VERSION,
        root=resolved_root,
        entrypoint=entrypoint,
        patch=patch,
        digest=digest,
    )


class DshProfileCapabilityHost:
    """Supervise one complete DSH Profile and its loopback MCP exporter."""

    def __init__(
        self,
        dsh_command: Sequence[str],
        *,
        projection: DshProfileProjection,
        dsh_home: Path,
        cwd: Path | None = None,
        environment: Mapping[str, str] | None = None,
        node_command: str | Path | None = None,
        startup_timeout: float = 15.0,
        shutdown_timeout: float = 5.0,
        health_timeout: float = 2.0,
        call_timeout: float = 60.0,
        max_argument_bytes: int = 256 * 1024,
        max_result_bytes: int = 1024 * 1024,
        max_request_bytes: int = 1024 * 1024,
        max_in_flight: int = 64,
        circuit_failure_threshold: int = 3,
        circuit_recovery_timeout: float = 5.0,
    ) -> None:
        self._command = self._validate_command(dsh_command)
        if not _PROFILE.fullmatch(projection.profile) or not _DIGEST.fullmatch(
            projection.config_digest
        ):
            raise PluginHostError(
                "dsh_capability_projection_invalid", "DSH Profile projection is invalid"
            )
        if projection.config_bytes < 0 or projection.config_bytes > _MAX_CONFIG_BYTES:
            raise PluginHostError(
                "dsh_capability_projection_invalid", "DSH Profile projection is too large"
            )
        if not _SEMVER.fullmatch(projection.host_version):
            raise PluginHostError(
                "dsh_capability_projection_invalid", "DSH Profile host version is invalid"
            )
        self._projection = projection
        self._dsh_home = dsh_home.expanduser().resolve()
        self._cwd = (cwd or Path.cwd()).expanduser().resolve()
        self._explicit_environment = self._validate_environment(environment or {})
        self._node_command = str(node_command).strip() if node_command is not None else None
        self._startup_timeout = self._positive_float(startup_timeout, "startup_timeout")
        self._shutdown_timeout = self._positive_float(shutdown_timeout, "shutdown_timeout")
        self._health_timeout = self._positive_float(health_timeout, "health_timeout")
        self._call_timeout_ms = self._milliseconds(call_timeout, "call_timeout", 3_600_000)
        self._max_argument_bytes = self._positive_int(
            max_argument_bytes, "max_argument_bytes", 8 * 1024 * 1024
        )
        self._max_result_bytes = self._positive_int(
            max_result_bytes, "max_result_bytes", 8 * 1024 * 1024
        )
        self._max_request_bytes = self._positive_int(
            max_request_bytes, "max_request_bytes", 8 * 1024 * 1024
        )
        self._max_in_flight = self._positive_int(max_in_flight, "max_in_flight", 1024)
        self._bundle = load_dsh_capability_bundle()
        self._circuit = DshCircuitBreaker(
            failure_threshold=circuit_failure_threshold,
            recovery_timeout=circuit_recovery_timeout,
        )
        self._lifecycle_lock = asyncio.Lock()
        self._process: asyncio.subprocess.Process | None = None
        self._stdout_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._monitor_task: asyncio.Task[None] | None = None
        self._ready_future: asyncio.Future[DshProfileCapabilityReady] | None = None
        self._token_future: asyncio.Future[str] | None = None
        self._runtime_dir: Path | None = None
        self._lease: DshMcpConnectorLease | None = None
        self._descriptor: DshProfileCapabilityDescriptor | None = None
        self._stdout_tail: deque[str] = deque(maxlen=64)
        self._stderr_tail: deque[str] = deque(maxlen=64)
        self._state: Literal["stopped", "starting", "ready", "failed", "disposed"] = "stopped"
        self._disposed = False
        self._stopping = False
        self._process_failure_recorded = False

    @property
    def pid(self) -> int | None:
        process = self._process
        return process.pid if process is not None and process.returncode is None else None

    @property
    def descriptor(self) -> DshProfileCapabilityDescriptor:
        if self._descriptor is None:
            raise PluginHostError(
                "dsh_capability_not_ready", "DSH capability descriptor is not ready"
            )
        return self._descriptor

    @property
    def circuit_snapshot(self) -> DshCircuitSnapshot:
        return self._circuit.snapshot()

    @property
    def stdout_tail(self) -> tuple[str, ...]:
        return tuple(self._stdout_tail)

    @property
    def stderr_tail(self) -> tuple[str, ...]:
        return tuple(self._stderr_tail)

    async def start(self) -> DshMcpConnectorLease:
        async with self._lifecycle_lock:
            if not _PROCESS_GROUP_ISOLATION_AVAILABLE:
                raise PluginHostError(
                    "dsh_capability_platform_unsupported",
                    "DSH capability host requires POSIX process-group isolation",
                )
            if self._disposed:
                raise PluginHostError(
                    "dsh_capability_host_disposed", "DSH capability host is disposed"
                )
            if self.pid is not None and self._state == "ready" and self._lease is not None:
                return self._lease
            if self._process is not None:
                await self._terminate()
            self._require_circuit_probe()
            self._state = "starting"
            self._process_failure_recorded = False
            try:
                environment = self._base_environment()
                await self._require_node_version(environment)
                await self._verify_projection(environment)
                runtime_dir = Path(tempfile.mkdtemp(prefix="ksadk-dsh-capability-"))
                runtime_dir.chmod(0o700)
                self._runtime_dir = runtime_dir
                overlay = runtime_dir / "cordis.patch.yml"
                overlay.write_text(self._overlay_text(), encoding="utf-8")
                overlay.chmod(0o600)
                environment.update(
                    {
                        "DSH_TELEMETRY_DISABLED": "1",
                        "KSADK_DSH_CAPABILITY_READY_STDOUT": "1",
                        "KSADK_DSH_PROFILE": self._projection.profile,
                        "KSADK_DSH_PROFILE_DIGEST": self._projection.config_digest,
                        "KSADK_DSH_VERSION": self._projection.host_version,
                    }
                )
                process = await asyncio.create_subprocess_exec(
                    *self._command,
                    "--profile",
                    self._projection.profile,
                    "--patch",
                    str(overlay),
                    cwd=str(self._cwd),
                    env=environment,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    start_new_session=True,
                )
                self._process = process
                ready_future = asyncio.get_running_loop().create_future()
                token_future = asyncio.get_running_loop().create_future()
                self._ready_future = ready_future
                self._token_future = token_future
                self._stdout_task = asyncio.create_task(
                    self._capture_stream(
                        process.stdout,
                        self._stdout_tail,
                        ready_future=ready_future,
                        token_future=token_future,
                    )
                )
                self._stderr_task = asyncio.create_task(
                    self._capture_stream(process.stderr, self._stderr_tail)
                )
                self._monitor_task = asyncio.create_task(self._monitor_process(process))
                ready, token = await self._wait_ready(
                    ready_future,
                    token_future,
                    process,
                )
                self._validate_ready_fences(ready)
                descriptor = DshProfileCapabilityDescriptor(
                    dsh_version=ready.dsh_version,
                    profile=ready.profile,
                    profile_digest=ready.profile_digest,
                    inventory_digest=ready.inventory_digest,
                    tools=ready.tools,
                )
                lease = DshMcpConnectorLease(
                    endpoint=ready.endpoint,
                    profile=ready.profile,
                    profile_digest=ready.profile_digest,
                    descriptor_digest=descriptor.descriptor_digest,
                    _bearer_token=token,
                )
                self._descriptor = descriptor
                self._lease = lease
                if not await self._probe_health(lease, descriptor):
                    raise PluginHostError(
                        "dsh_capability_health_failed",
                        "DSH capability MCP endpoint failed its startup health check",
                    )
                self._state = "ready"
                self._circuit.succeed()
                return lease
            except asyncio.CancelledError:
                await self._terminate()
                self._state = "stopped"
                raise
            except PluginHostError:
                self._record_process_failure()
                await self._terminate()
                self._state = "failed"
                raise
            except Exception as error:
                self._record_process_failure()
                await self._terminate()
                self._state = "failed"
                raise PluginHostError(
                    "dsh_capability_start_failed",
                    f"DSH capability host failed to start: {type(error).__name__}",
                ) from error

    async def lease(self) -> DshMcpConnectorLease:
        return await self.start()

    async def health(self) -> bool:
        lease = self._lease
        descriptor = self._descriptor
        if self.pid is None or lease is None or descriptor is None or self._state != "ready":
            return False
        healthy = await self._probe_health(lease, descriptor)
        if healthy:
            self._circuit.succeed()
        else:
            self._circuit.fail()
            self._state = "failed"
        return healthy

    async def inventory(self) -> DshProfileCapabilityInventory:
        descriptor = self._descriptor
        state = self._state
        if state == "ready" and not await self.health():
            state = "failed"
        snapshot = self._circuit.snapshot()
        return DshProfileCapabilityInventory(
            profile=self._projection.profile,
            profile_digest=self._projection.config_digest,
            descriptor_digest=descriptor.descriptor_digest if descriptor is not None else None,
            inventory_digest=descriptor.inventory_digest if descriptor is not None else None,
            state=state,
            pid=self.pid,
            tool_count=len(descriptor.tools) if descriptor is not None else 0,
            circuit_state=snapshot.state,
            consecutive_failures=snapshot.consecutive_failures,
            retry_after_seconds=snapshot.retry_after_seconds,
        )

    async def dispose(self) -> None:
        async with self._lifecycle_lock:
            if self._disposed:
                return
            self._disposed = True
            await self._terminate()
            self._state = "disposed"

    def _overlay_text(self) -> str:
        entrypoint = json.dumps(self._bundle.entrypoint.as_uri())
        return (
            "- insert:\n"
            "    - id: ksadk-dsh-capability-host\n"
            f"      name: {entrypoint}\n"
            "      config:\n"
            f"        callTimeoutMs: {self._call_timeout_ms}\n"
            f"        maxArgumentBytes: {self._max_argument_bytes}\n"
            f"        maxResultBytes: {self._max_result_bytes}\n"
            f"        maxRequestBytes: {self._max_request_bytes}\n"
            f"        maxInFlight: {self._max_in_flight}\n"
        )

    def _base_environment(self) -> dict[str, str]:
        environment = cast(dict[str, str], dsh_subprocess_environment(dsh_home=self._dsh_home))
        environment.update(self._explicit_environment)
        # The caller cannot inject NODE_OPTIONS, but the supervised sidecar gets
        # a fixed heap ceiling. This also applies to Cordis plugin child Nodes.
        environment["NODE_OPTIONS"] = f"--max-old-space-size={_NODE_MEMORY_LIMIT_MB}"
        return environment

    async def _require_node_version(self, environment: Mapping[str, str]) -> None:
        executable = self._node_command
        if executable is None:
            executable = shutil.which("node", path=environment.get("PATH"))
        if not executable or "\x00" in executable:
            raise PluginHostError(
                "dsh_capability_node_unavailable", "Node.js is required for DSH capabilities"
            )
        candidate = Path(executable).expanduser()
        if candidate.is_absolute() or len(candidate.parts) > 1:
            if not candidate.is_file():
                raise PluginHostError(
                    "dsh_capability_node_unavailable", "Node.js executable is unavailable"
                )
            executable = str(candidate.resolve())
        try:
            process = await asyncio.create_subprocess_exec(
                executable,
                "--version",
                cwd=str(self._cwd),
                env=dict(environment),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as error:
            raise PluginHostError(
                "dsh_capability_node_unavailable", "Node.js version probe failed"
            ) from error
        try:
            stdout, _stderr = await asyncio.wait_for(
                process.communicate(), timeout=self._health_timeout
            )
        except asyncio.CancelledError:
            if process.returncode is None:
                process.kill()
                await process.wait()
            raise
        except asyncio.TimeoutError as error:
            if process.returncode is None:
                process.kill()
                await process.wait()
            raise PluginHostError(
                "dsh_capability_node_unavailable", "Node.js version probe failed"
            ) from error
        match = _NODE_VERSION.fullmatch(stdout.decode("utf-8", errors="replace").strip())
        if process.returncode != 0 or match is None:
            raise PluginHostError(
                "dsh_capability_node_unavailable", "Node.js did not report a valid version"
            )
        version = tuple(int(value) for value in match.groups())
        if version < DSH_CAPABILITY_MIN_NODE_VERSION:
            minimum = ".".join(str(value) for value in DSH_CAPABILITY_MIN_NODE_VERSION)
            raise PluginHostError(
                "dsh_capability_node_version_unsupported",
                f"DSH capabilities require Node.js >= {minimum}",
            )

    async def _verify_projection(self, environment: Mapping[str, str]) -> None:
        try:
            process = await asyncio.create_subprocess_exec(
                *self._command,
                "--profile",
                self._projection.profile,
                "--dump-config",
                cwd=str(self._cwd),
                env=dict(environment),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=os.name == "posix",
            )
        except OSError as error:
            raise PluginHostError(
                "dsh_capability_profile_unavailable", "DSH Profile preflight did not complete"
            ) from error
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=self._startup_timeout
            )
        except asyncio.CancelledError:
            await self._terminate_process_tree(process)
            raise
        except asyncio.TimeoutError as error:
            await self._terminate_process_tree(process)
            raise PluginHostError(
                "dsh_capability_profile_unavailable", "DSH Profile preflight did not complete"
            ) from error
        self._append_diagnostic(stderr, self._stderr_tail)
        digest = f"sha256:{hashlib.sha256(stdout).hexdigest()}"
        if process.returncode != 0:
            raise PluginHostError(
                "dsh_capability_profile_unavailable", "DSH Profile preflight failed"
            )
        if len(stdout) != self._projection.config_bytes or digest != self._projection.config_digest:
            raise PluginHostError(
                "dsh_capability_profile_changed",
                "DSH Profile changed after its immutable projection was selected",
            )

    async def _wait_ready(
        self,
        ready_future: asyncio.Future[DshProfileCapabilityReady],
        token_future: asyncio.Future[str],
        process: asyncio.subprocess.Process,
    ) -> tuple[DshProfileCapabilityReady, str]:
        deadline = asyncio.get_running_loop().time() + self._startup_timeout
        while asyncio.get_running_loop().time() < deadline:
            if ready_future.done() and token_future.done():
                return ready_future.result(), token_future.result()
            if process.returncode is not None:
                raise PluginHostError(
                    "dsh_capability_host_exited", "DSH capability host exited before readiness"
                )
            await asyncio.sleep(0.02)
        raise PluginHostError(
            "dsh_capability_start_timeout", "DSH capability host did not become ready in time"
        )

    def _validate_ready_fences(self, ready: DshProfileCapabilityReady) -> None:
        if (
            ready.host_version != DSH_CAPABILITY_HOST_VERSION
            or ready.dsh_version != self._projection.host_version
            or ready.profile != self._projection.profile
            or ready.profile_digest != self._projection.config_digest
        ):
            raise PluginHostError(
                "dsh_capability_ready_mismatch",
                "DSH capability readiness crossed its Profile or version fence",
            )

    async def _probe_health(
        self,
        lease: DshMcpConnectorLease,
        descriptor: DshProfileCapabilityDescriptor,
    ) -> bool:
        parsed = urlsplit(lease.endpoint)
        health_endpoint = f"http://127.0.0.1:{parsed.port}/health"
        try:
            async with httpx.AsyncClient(
                headers=lease.headers(), timeout=self._health_timeout, trust_env=False
            ) as client:
                response = await client.get(health_endpoint)
            if len(response.content) > 64 * 1024:
                return False
            payload = response.json()
        except (httpx.HTTPError, ValueError, TypeError):
            return False
        return bool(
            response.status_code == 200
            and isinstance(payload, dict)
            and payload.get("protocolVersion") == DSH_CAPABILITY_HOST_PROTOCOL
            and payload.get("healthy") is True
            and payload.get("profile") == descriptor.profile
            and payload.get("profileDigest") == descriptor.profile_digest
            and payload.get("inventoryDigest") == descriptor.inventory_digest
            and payload.get("toolCount") == len(descriptor.tools)
            and payload.get("drifted") is False
            and payload.get("draining") is False
        )

    async def _capture_stream(
        self,
        stream: asyncio.StreamReader | None,
        target: deque[str],
        *,
        ready_future: asyncio.Future[DshProfileCapabilityReady] | None = None,
        token_future: asyncio.Future[str] | None = None,
    ) -> None:
        if stream is None:
            return
        ready_prefix = _READY_PREFIX.encode("ascii")
        token_prefix = _TOKEN_PREFIX.encode("ascii")
        remainder = bytearray()
        discard_oversized_ready = False
        while True:
            chunk = await stream.read(4096)
            if not chunk:
                break
            if discard_oversized_ready:
                separator = chunk.find(b"\n")
                if separator < 0:
                    continue
                chunk = chunk[separator + 1 :]
                discard_oversized_ready = False
            remainder.extend(chunk)
            while True:
                separator = remainder.find(b"\n")
                if separator < 0:
                    break
                line = bytes(remainder[:separator])
                del remainder[: separator + 1]
                if line.startswith(ready_prefix):
                    self._capture_ready(line, ready_future)
                elif line.startswith(token_prefix):
                    self._capture_token(line, token_future)
                else:
                    self._append_diagnostic(line, target)
            if remainder.startswith(ready_prefix):
                if len(remainder) > len(ready_prefix) + _MAX_READY_BYTES:
                    self._capture_ready(bytes(remainder), ready_future)
                    remainder.clear()
                    discard_oversized_ready = True
            elif (
                not ready_prefix.startswith(remainder)
                and len(remainder) > _MAX_DIAGNOSTIC_CHARS
            ):
                self._append_diagnostic(bytes(remainder), target)
                remainder.clear()
        if remainder:
            line = bytes(remainder)
            if line.startswith(ready_prefix):
                self._capture_ready(line, ready_future)
            elif line.startswith(token_prefix):
                self._capture_token(line, token_future)
            else:
                self._append_diagnostic(line, target)

    @staticmethod
    def _capture_ready(
        line: bytes,
        future: asyncio.Future[DshProfileCapabilityReady] | None,
    ) -> None:
        if future is None or future.done():
            return
        encoded = line[len(_READY_PREFIX.encode("ascii")) :]
        try:
            if not encoded or len(encoded) > _MAX_READY_BYTES:
                raise ValueError("readiness payload is empty or oversized")
            payload = json.loads(encoded.decode("utf-8", errors="strict"))
            ready = DshProfileCapabilityReady.model_validate(payload)
        except (UnicodeError, json.JSONDecodeError, ValueError):
            future.set_exception(
                PluginHostError(
                    "dsh_capability_ready_invalid",
                    "DSH capability readiness record is invalid",
                )
            )
            return
        future.set_result(ready)

    @staticmethod
    def _capture_token(line: bytes, future: asyncio.Future[str] | None) -> None:
        if future is None or future.done():
            return
        try:
            token = line[len(_TOKEN_PREFIX.encode("ascii")) :].decode(
                "ascii", errors="strict"
            )
            if re.fullmatch(r"[A-Za-z0-9_-]{32,128}", token) is None:
                raise ValueError("invalid runtime token")
        except (UnicodeError, ValueError):
            future.set_exception(
                PluginHostError(
                    "dsh_capability_ready_invalid",
                    "DSH capability readiness record is invalid",
                )
            )
            return
        future.set_result(token)

    async def _monitor_process(self, process: asyncio.subprocess.Process) -> None:
        await process.wait()
        async with self._lifecycle_lock:
            if process is self._process and not self._stopping and not self._disposed:
                self._state = "failed"
                self._record_process_failure()
                # The group leader may exit while Cordis/plugin children keep
                # running. Reap the process group immediately so a stale PGID
                # is neither leaked nor reused before the next lifecycle call.
                await self._terminate()

    async def _terminate(self) -> None:
        process = self._process
        self._stopping = True
        try:
            if process is not None:
                await self._terminate_process_tree(process)
            tasks = [self._stdout_task, self._stderr_task, self._monitor_task]
            for task in tasks:
                if task is None or task is asyncio.current_task():
                    continue
                try:
                    await asyncio.wait_for(task, timeout=self._shutdown_timeout)
                except asyncio.TimeoutError:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
        finally:
            self._process = None
            self._stdout_task = None
            self._stderr_task = None
            self._monitor_task = None
            ready_future = self._ready_future
            self._ready_future = None
            if ready_future is not None and not ready_future.done():
                ready_future.cancel()
            token_future = self._token_future
            self._token_future = None
            if token_future is not None and not token_future.done():
                token_future.cancel()
            self._lease = None
            self._descriptor = None
            runtime_dir = self._runtime_dir
            self._runtime_dir = None
            if runtime_dir is not None and runtime_dir.name.startswith("ksadk-dsh-capability-"):
                shutil.rmtree(runtime_dir, ignore_errors=True)
            self._stopping = False

    async def _terminate_process_tree(self, process: asyncio.subprocess.Process) -> None:
        """Stop the isolated sidecar session, including plugin child processes."""

        deadline = asyncio.get_running_loop().time() + self._shutdown_timeout
        if os.name == "posix":
            self._signal_process_group(process.pid, signal.SIGTERM)
        elif process.returncode is None:
            process.terminate()

        if process.returncode is None:
            try:
                remaining = max(0.001, deadline - asyncio.get_running_loop().time())
                await asyncio.wait_for(process.wait(), timeout=remaining)
            except asyncio.TimeoutError:
                pass

        if os.name == "posix":
            while self._process_group_exists(process.pid):
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    break
                await asyncio.sleep(min(0.05, remaining))
            if self._process_group_exists(process.pid):
                self._signal_process_group(process.pid, signal.SIGKILL)
        elif process.returncode is None:
            process.kill()

        if process.returncode is None:
            await process.wait()

    @staticmethod
    def _signal_process_group(process_group: int, signum: int) -> None:
        try:
            os.killpg(process_group, signum)
        except ProcessLookupError:
            return

    @staticmethod
    def _process_group_exists(process_group: int) -> bool:
        try:
            os.killpg(process_group, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    def _record_process_failure(self) -> None:
        if self._process_failure_recorded:
            return
        self._process_failure_recorded = True
        self._circuit.fail()

    def _require_circuit_probe(self) -> None:
        if self._circuit.acquire():
            return
        snapshot = self._circuit.snapshot()
        raise PluginHostError(
            "dsh_capability_circuit_open",
            "DSH capability host circuit is open; wait "
            f"{snapshot.retry_after_seconds:.1f}s before another probe",
        )

    @staticmethod
    def _append_diagnostic(value: bytes, target: deque[str]) -> None:
        text = value.decode("utf-8", errors="replace").strip()[:_MAX_DIAGNOSTIC_CHARS]
        if text:
            target.append(_DIAGNOSTIC_SECRET.sub("[REDACTED]", text))

    @staticmethod
    def _validate_command(command: Sequence[str]) -> tuple[str, ...]:
        if isinstance(command, (str, bytes)):
            raise PluginHostError(
                "dsh_capability_command_invalid", "DSH command must be an argv sequence"
            )
        normalized = tuple(str(value) for value in command)
        if not normalized or any(not value or "\x00" in value for value in normalized):
            raise PluginHostError(
                "dsh_capability_command_invalid", "DSH command contains an invalid argv item"
            )
        return normalized

    @staticmethod
    def _validate_environment(environment: Mapping[str, str]) -> dict[str, str]:
        normalized: dict[str, str] = {}
        for key, value in environment.items():
            if (
                not _ENV_NAME.fullmatch(key)
                or key in _BLOCKED_ENV
                or key.startswith("DYLD_")
                or _SECRET_ENV.search(key)
            ):
                raise PluginHostError(
                    "dsh_capability_environment_denied",
                    f"DSH capability environment key {key!r} is not allowed",
                )
            text = str(value)
            if "\x00" in text:
                raise PluginHostError(
                    "dsh_capability_environment_denied", "DSH capability environment is invalid"
                )
            normalized[key] = text
        return normalized

    @staticmethod
    def _positive_float(value: float, field_name: str) -> float:
        normalized = float(value)
        if normalized <= 0:
            raise PluginHostError("dsh_capability_limit_invalid", f"{field_name} must be positive")
        return normalized

    @classmethod
    def _milliseconds(cls, value: float, field_name: str, maximum: int) -> int:
        milliseconds = round(cls._positive_float(value, field_name) * 1000)
        if milliseconds < 1 or milliseconds > maximum:
            raise PluginHostError(
                "dsh_capability_limit_invalid", f"{field_name} is outside the supported range"
            )
        return milliseconds

    @staticmethod
    def _positive_int(value: int, field_name: str, maximum: int) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < 1 or value > maximum:
            raise PluginHostError(
                "dsh_capability_limit_invalid", f"{field_name} is outside the supported range"
            )
        return value


__all__ = [
    "DSH_CAPABILITY_BUNDLE_PACKAGE",
    "DSH_CAPABILITY_DEFINITION",
    "DSH_CAPABILITY_HOST_PROTOCOL",
    "DSH_CAPABILITY_HOST_VERSION",
    "DSH_CAPABILITY_MCP_PROTOCOL",
    "DSH_CAPABILITY_MIN_NODE_VERSION",
    "DSH_CAPABILITY_TRANSPORT",
    "DshCapabilityBundle",
    "DshCapabilityTool",
    "DshMcpConnectorLease",
    "DshProfileCapabilityDescriptor",
    "DshProfileCapabilityHost",
    "DshProfileCapabilityInventory",
    "DshProfileCapabilityReady",
    "load_dsh_capability_bundle",
]

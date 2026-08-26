"""System-independent manifest bundles for platform-managed runtimes."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import yaml

from ksadk.builders.base import BaseBuilder, BuildResult

RUNTIME_MANIFEST_SCHEMA = "runtime-manifest/v1"
_MANIFEST_KEYS = (
    "name",
    "version",
    "framework",
    "artifact_type",
    "runtime",
    "model",
    "models",
    "prompt",
    "task_prompt",
    "skills",
    "mcp_servers",
    "sandbox",
    "approval_mode",
    "context",
    "memory",
)


def managed_runtime_lock_path(manifest_path: Path) -> Path:
    """Return the immutable lock that accompanies a YAML-only declaration.

    ``ManagedRuntime`` is not a user-code artifact.  Keeping its two small
    declaration files next to one another makes that visible in both the
    workspace and the build receipt, while still preserving a historical
    manifest for rollback.
    """

    return manifest_path.with_suffix(".lock.json")


def serialize_managed_runtime_manifest(manifest: dict[str, Any]) -> bytes:
    """Serialize the Server-canonical ManagedRuntime declaration.

    The Server validates ``ManifestSHA256`` after parsing and re-dumping YAML
    with sorted keys.  Clients must hash those exact canonical bytes instead
    of the editable source formatting, otherwise a valid Studio/CLI build is
    rejected during ``CreateAgent``/``UpdateAgent`` admission.
    """

    canonical = yaml.safe_dump(
        manifest,
        allow_unicode=True,
        sort_keys=True,
        default_flow_style=False,
        width=10_000,
    )
    if not canonical.endswith("\n"):
        canonical += "\n"
    return canonical.encode("utf-8")


class ManagedRuntimeBuilder(BaseBuilder):
    """Build a deterministic YAML-only deployment artifact.

    Runtime dependencies and local credentials intentionally never enter this
    artifact. The managed runtime image supplies executable dependencies while
    AgentEngine injects deployment-time credentials.
    """

    def __init__(
        self,
        project_dir: Path,
        config: dict[str, Any] | None = None,
        *,
        runtime_version: str | None = None,
    ) -> None:
        super().__init__(project_dir, config)
        self.runtime_version = str(runtime_version or "").strip()
        self.build_dir = self.project_dir / ".agentengine" / "managed_runtime"

    def build(self) -> BuildResult:
        # An explicitly supplied snapshot is authoritative.  Studio can keep
        # more than one Agent manifest in a workspace, so falling back to the
        # root agentengine.yaml here would silently build the wrong Agent.
        config = dict(self.config) if self.config else self._load_config()
        error = self._validate_config(config)
        if error:
            return BuildResult(success=False, error_message=error)

        runtime = dict(config.get("runtime") or {})
        version = self.runtime_version or str(runtime.get("version") or "").strip()
        if not version:
            return BuildResult(
                success=False,
                error_message=(
                    "ManagedRuntime 构建需要已解析的 runtime.version；"
                    "请显式配置版本或连接 AgentEngine 获取服务端默认版本"
                ),
            )

        runtime_name = str(runtime.get("name") or config.get("framework") or "").strip().lower()
        runtime = {"name": runtime_name, "version": version}
        manifest = self._normalized_manifest(config, runtime)
        manifest_bytes = serialize_managed_runtime_manifest(manifest)
        manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
        lock = {
            "schema_version": RUNTIME_MANIFEST_SCHEMA,
            "runtime": runtime,
            "manifest_sha256": manifest_sha256,
        }
        lock_bytes = (json.dumps(lock, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
            "utf-8"
        )

        self.build_dir.mkdir(parents=True, exist_ok=True)
        name = str(config.get("name") or self.project_dir.name).strip() or self.project_dir.name
        project_version = str(config.get("version") or "1.0.0").strip() or "1.0.0"
        # This local declaration receipt is retained for Studio rollback.
        # It is deliberately *not* a ZIP: YAML agents have no user code, no
        # KS3 artifact and no code-downloader path.  Version alone is mutable
        # in an editable Agent, so retain the exact canonical YAML plus its
        # lock under the content digest.
        artifact_path = self.build_dir / (
            f"{name}-{project_version}-{manifest_sha256[:16]}-runtime.yaml"
        )
        lock_path = managed_runtime_lock_path(artifact_path)
        artifact_path.write_bytes(manifest_bytes)
        lock_path.write_bytes(lock_bytes)
        return BuildResult(
            success=True,
            artifact_path=artifact_path,
            artifact_size=artifact_path.stat().st_size + lock_path.stat().st_size,
            metadata={
                "agent_name": name,
                "framework": str(config.get("framework") or ""),
                "artifact_type": "ManagedRuntime",
                "runtime_name": runtime_name,
                "runtime_version": version,
                "manifest_sha256": manifest_sha256,
            },
        )

    @staticmethod
    def _validate_config(config: dict[str, Any]) -> str | None:
        framework = str(config.get("framework") or "").strip().lower()
        artifact_type = str(config.get("artifact_type") or "").strip().lower()
        runtime = config.get("runtime")
        if framework != "codex":
            return "当前 ManagedRuntime v1 仅支持 framework: codex"
        if artifact_type != "managedruntime":
            return "ManagedRuntime 项目必须配置 artifact_type: ManagedRuntime"
        if not isinstance(runtime, dict):
            return "ManagedRuntime 项目必须配置 runtime.name"
        runtime_name = str(runtime.get("name") or "").strip().lower()
        if runtime_name != "codex":
            return "当前 ManagedRuntime v1 仅支持 runtime.name: codex"
        return None

    @staticmethod
    def _normalized_manifest(
        config: dict[str, Any],
        runtime: dict[str, str],
    ) -> dict[str, Any]:
        normalized: dict[str, Any] = {}
        for key in _MANIFEST_KEYS:
            if key == "runtime":
                normalized[key] = runtime
            elif key == "artifact_type":
                normalized[key] = "ManagedRuntime"
            elif key in config:
                normalized[key] = config[key]
        return normalized

"""Cloud restoration of one frozen Codex marketplace before serving traffic.

The control plane admits immutable artifact references.  The platform init
container downloads and extracts the selected archive from internal KS3; the
runtime verifies that materialized tree before it becomes visible to Codex.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from ksadk.builders.managed_runtime_builder import serialize_managed_runtime_manifest
from ksadk.codex.client import CodexPluginBootstrap
from ksadk.plugins.artifacts import (
    PluginArtifactReceipt,
    restore_materialized_plugin_artifact,
    restore_plugin_artifact,
)
from ksadk.resource_runtime.managed_projection import native_codex_plugin_bindings


class PluginDelivery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["ksadk.plugin-delivery/v1"] = "ksadk.plugin-delivery/v1"
    artifact_id: str | None = Field(default=None, pattern=r"^[0-9a-f-]{36}$")
    artifact_path: str = Field(pattern=r"^ks3://[a-z0-9][a-z0-9.-]*/[^\s]+\.zip$")
    storage_region: str = Field(min_length=1, max_length=100)
    receipt: PluginArtifactReceipt
    build_id: str = Field(pattern=r"^build_[0-9a-f]{8,64}$")
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    marketplace_name: str = Field(min_length=1, max_length=256)
    plugin_names: list[str] = Field(min_length=1, max_length=128)
    snapshot_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    bindings: list[dict[str, Any]] = Field(min_length=1, max_length=128)

    def validate_manifest(self, manifest: dict[str, Any]) -> None:
        digest = hashlib.sha256(serialize_managed_runtime_manifest(manifest)).hexdigest()
        if digest != self.manifest_sha256:
            raise ValueError("Plugin delivery belongs to a different manifest")
        runtime = manifest.get("runtime") or {}
        if runtime.get("name") != "codex" or runtime.get("version") != self.receipt.runtime_version:
            raise ValueError("Plugin delivery runtime does not match the manifest")
        bindings = native_codex_plugin_bindings(manifest)
        if self.bindings != bindings:
            raise ValueError("Plugin delivery does not match the selected components")

    def bootstrap(self, path: Path) -> CodexPluginBootstrap:
        return CodexPluginBootstrap(
            marketplace_path=str(path.resolve()),
            marketplace_name=self.marketplace_name,
            plugin_names=tuple(self.plugin_names),
            snapshot_digest=self.snapshot_digest,
        )


def restore_delivery(delivery: PluginDelivery, archive: Path, work_dir: Path) -> dict[str, Any]:
    """Restore into a content-addressed directory; recheck cached bytes on restart."""
    if (
        archive.stat().st_size != delivery.receipt.size_bytes
        or "sha256:" + hashlib.sha256(archive.read_bytes()).hexdigest()
        != delivery.receipt.artifact_digest
    ):
        raise ValueError("Downloaded plugin artifact does not match its receipt")
    target = work_dir / ".agentkit" / "cloud-plugins" / delivery.receipt.artifact_digest[7:]
    bootstrap = delivery.bootstrap(target)
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".plugin-publish-", dir=target.parent) as tmp:
            staged = Path(tmp) / "marketplace"
            restore_plugin_artifact(archive, delivery.receipt, staged)
            try:
                os.rename(staged, target)
            except OSError as exc:
                # Concurrent workers can publish the same immutable tree. Only
                # an existing destination is recoverable; verify its bytes below.
                if exc.errno not in {errno.EEXIST, errno.ENOTEMPTY} or not target.is_dir():
                    raise
    bootstrap.verify()
    return {
        "codex_home_key": delivery.build_id,
        "codex_plugin_bootstrap": {
            "marketplace_path": str(target.resolve()),
            "marketplace_name": delivery.marketplace_name,
            "plugin_names": delivery.plugin_names,
            "snapshot_digest": delivery.snapshot_digest,
        },
    }


def restore_materialized_delivery(
    delivery: PluginDelivery, source: Path, work_dir: Path
) -> dict[str, Any]:
    """Install a platform-downloaded tree into the workload-scoped cache."""
    target = work_dir / ".agentkit" / "cloud-plugins" / delivery.receipt.artifact_digest[7:]
    bootstrap = delivery.bootstrap(target)
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".plugin-publish-", dir=target.parent) as tmp:
            staged = Path(tmp) / "marketplace"
            restore_materialized_plugin_artifact(source, delivery.receipt, staged)
            try:
                os.rename(staged, target)
            except OSError as exc:
                if exc.errno not in {errno.EEXIST, errno.ENOTEMPTY} or not target.is_dir():
                    raise
    bootstrap.verify()
    return {
        "codex_home_key": delivery.build_id,
        "codex_plugin_bootstrap": {
            "marketplace_path": str(target.resolve()),
            "marketplace_name": delivery.marketplace_name,
            "plugin_names": delivery.plugin_names,
            "snapshot_digest": delivery.snapshot_digest,
        },
    }


def prepare_cloud_plugins(manifest: dict[str, Any], work_dir: Path) -> dict[str, Any]:
    """Verify and activate the init-container-materialized plugin artifact."""
    bindings = native_codex_plugin_bindings(manifest)
    if not bindings:
        return {}
    raw = os.environ.get("AGENTENGINE_PLUGIN_DELIVERY", "")
    if not raw:
        raise ValueError("Native plugins require an admitted cloud delivery reference")
    delivery = PluginDelivery.model_validate_json(raw)
    delivery.validate_manifest(manifest)
    source_value = os.environ.get("AGENTENGINE_PLUGIN_ARTIFACT_DIR", "").strip()
    if not source_value:
        raise ValueError("Native plugins require a materialized cloud artifact")
    source = Path(source_value)
    work_dir.mkdir(parents=True, exist_ok=True)
    config = restore_materialized_delivery(delivery, source, work_dir)
    # Only non-secret, verified launch data is persisted. Recreated each startup.
    path = work_dir / ".agentkit" / "cloud-plugin-launch.json"
    with tempfile.NamedTemporaryFile(
        mode="w", dir=path.parent, prefix=".plugin-launch-", delete=False
    ) as stream:
        temp = Path(stream.name)
        json.dump({"manifest_sha256": delivery.manifest_sha256, "config": config}, stream)
    try:
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)
    return config

"""Cloud restoration of one frozen Codex marketplace before serving traffic.

The control plane owns artifact references and workload authentication. Signed
URLs and credentials are transport data, never part of the Build or cache key.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field

from ksadk.builders.managed_runtime_builder import serialize_managed_runtime_manifest
from ksadk.codex.client import CodexPluginBootstrap
from ksadk.plugins.artifacts import PluginArtifactReceipt, restore_plugin_artifact


class PluginDelivery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["ksadk.plugin-delivery/v1"] = "ksadk.plugin-delivery/v1"
    artifact_id: str = Field(pattern=r"^[0-9a-f-]{36}$")
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
        bindings = [b for b in manifest.get("plugins", []) if b.get("enabled", True)]
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


def prepare_cloud_plugins(manifest: dict[str, Any], work_dir: Path) -> dict[str, Any]:
    """Resolve the current AgentVersion's references using its existing API key."""
    bindings = [b for b in manifest.get("plugins", []) if b.get("enabled", True)]
    if not bindings:
        return {}
    raw = os.environ.get("AGENTENGINE_PLUGIN_DELIVERY", "")
    if not raw:
        raise ValueError("Native plugins require an admitted cloud delivery reference")
    delivery = PluginDelivery.model_validate_json(raw)
    delivery.validate_manifest(manifest)
    control_url = os.environ.get("AGENTENGINE_PLUGIN_CONTROL_URL", "").rstrip("/")
    agent_id = os.environ.get("AGENTENGINE_PLUGIN_AGENT_ID", "")
    api_key = os.environ.get("AGENTENGINE_PLUGIN_API_KEY", "")
    if urlsplit(control_url).scheme != "https" or not agent_id or not api_key:
        raise ValueError("Plugin delivery requires HTTPS control plane and workload credentials")
    # The bearer belongs only to the control plane. Never forward it to KS3.
    with httpx.Client(timeout=60, follow_redirects=False, trust_env=False) as client:
        for attempt in range(6):
            try:
                response = client.post(
                    control_url + "/agentengine/api/v1/ResolveAgentPluginArtifact",
                    headers={"Authorization": "Bearer " + api_key},
                    json={"AgentId": agent_id, "ManifestSHA256": delivery.manifest_sha256},
                )
            except httpx.TransportError:
                if attempt == 5:
                    raise
            else:
                if response.status_code not in {409, 429, 502, 503, 504} or attempt == 5:
                    break
            time.sleep(min(2**attempt, 8))
        response.raise_for_status()
        result = response.json()
        if result.get("Code", 0) != 0:
            raise ValueError("Control plane refused plugin delivery")
        data = result.get("Data") or {}
        if (
            data.get("ArtifactId") != delivery.artifact_id
            or data.get("Receipt") != delivery.receipt.model_dump()
        ):
            raise ValueError("Control plane plugin receipt does not match the pinned version")
        url = data.get("DownloadUrl") or ""
        if urlsplit(url).scheme != "https":
            raise ValueError("Plugin artifact download must use HTTPS")
        work_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".plugin-download-", dir=work_dir) as tmp:
            archive = Path(tmp) / "artifact.zip"
            size = 0
            with client.stream("GET", url) as download, archive.open("wb") as out:
                download.raise_for_status()
                for chunk in download.iter_bytes():
                    size += len(chunk)
                    if size > delivery.receipt.size_bytes:
                        raise ValueError("Plugin download exceeds pinned size")
                    out.write(chunk)
            if size != delivery.receipt.size_bytes:
                raise ValueError("Plugin download is incomplete")
            config = restore_delivery(delivery, archive, work_dir)
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

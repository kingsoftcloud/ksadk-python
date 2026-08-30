"""Local manifest assembly for server-managed runtime deployments."""

from __future__ import annotations

from pathlib import Path

from ksadk.builders.managed_runtime_builder import ManagedRuntimeBuilder
from ksadk.deployment.base import DeployTarget, PackageInfo


def build_managed_runtime_package(
    package_info: PackageInfo, target: DeployTarget
) -> PackageInfo:
    """Build the canonical manifest and retain its integrity digest for deploy."""

    builder = ManagedRuntimeBuilder(
        Path(package_info.project_dir),
        runtime_version=str(target.extra.get("runtime_version") or ""),
    )
    result = builder.build()
    if not result.success:
        raise RuntimeError(f"构建失败: {result.error_message}")

    package_info.metadata.update(result.metadata)
    manifest_sha256 = str(result.metadata.get("manifest_sha256") or "").strip()
    if manifest_sha256:
        target.extra["manifest_sha256"] = manifest_sha256
    if result.artifact_path is not None:
        package_info.metadata["managed_manifest_path"] = str(result.artifact_path)
        # ManagedRuntime deployment submits this exact YAML declaration to
        # Server; it has no code ZIP, KS3 upload or CodeConfig.
        package_info.metadata["managed_runtime_manifest"] = result.artifact_path.read_text(
            encoding="utf-8"
        )
    return package_info


__all__ = ["build_managed_runtime_package"]

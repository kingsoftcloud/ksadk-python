"""Managed runtime configuration and version resolution."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as package_version
from typing import Any


class ManagedRuntimeError(ValueError):
    """Raised when a managed runtime contract cannot be resolved safely."""


@dataclass(frozen=True)
class ResolvedRuntime:
    name: str
    version: str
    source: str
    package_requirement: str = ""


def runtime_config(config: dict[str, Any]) -> tuple[str, str]:
    value = config.get("runtime")
    if not isinstance(value, dict):
        raise ManagedRuntimeError("ManagedRuntime 项目缺少 runtime 配置")
    name = str(value.get("name") or "").strip().lower()
    requested_version = str(value.get("version") or "").strip()
    if not name:
        raise ManagedRuntimeError("ManagedRuntime 项目缺少 runtime.name")
    return name, requested_version


def extract_bootstrap_runtime(
    bootstrap: dict[str, Any] | None,
    runtime_name: str,
) -> ResolvedRuntime | None:
    if not isinstance(bootstrap, dict):
        return None
    configs = bootstrap.get("configs") or bootstrap.get("Configs")
    if not isinstance(configs, dict):
        return None
    version = str(
        configs.get("runtime.default_version")
        or configs.get(f"runtime.{runtime_name}.default_version")
        or ""
    ).strip()
    if not version:
        return None
    package = str(
        configs.get("runtime.package")
        or configs.get(f"runtime.{runtime_name}.package")
        or ""
    ).strip()
    return ResolvedRuntime(
        name=runtime_name,
        version=version,
        source="server",
        package_requirement=package,
    )


async def resolve_managed_runtime(
    config: dict[str, Any],
    *,
    region: str,
    bootstrap: dict[str, Any] | None = None,
) -> ResolvedRuntime:
    name, requested_version = runtime_config(config)
    if requested_version:
        return ResolvedRuntime(name=name, version=requested_version, source="manifest")

    resolved = extract_bootstrap_runtime(bootstrap, name)
    if resolved is not None:
        return resolved

    if bootstrap is None:
        from ksadk.api.client import AgentEngineClient
        from ksadk.version import VERSION as CLI_VERSION

        try:
            async with AgentEngineClient(region=region) as client:
                payload = await client.get_client_bootstrap_config(
                    product="agentengine",
                    framework=name,
                    region=region,
                    client_type="cli",
                    client_version=CLI_VERSION,
                    ignore_dry_run=True,
                )
        except Exception as exc:
            raise ManagedRuntimeError(
                "无法从 AgentEngine 获取 ManagedRuntime 默认版本；"
                "请连接服务端或在 agentengine.yaml 中显式配置 runtime.version"
            ) from exc
        resolved = extract_bootstrap_runtime(payload, name)
        if resolved is not None:
            return resolved

    raise ManagedRuntimeError(
        f"AgentEngine 未下发 {name} 的默认 Runtime 版本；"
        "请配置服务端 Runtime catalog 或显式设置 runtime.version"
    )


def installed_runtime_version(runtime_name: str) -> str:
    package_name = {"codex": "openai-codex"}.get(runtime_name, runtime_name)
    try:
        return package_version(package_name)
    except PackageNotFoundError:
        return ""


def validate_installed_runtime(resolved: ResolvedRuntime) -> str:
    installed = installed_runtime_version(resolved.name)
    if not installed:
        raise ManagedRuntimeError(
            f"本地缺少 {resolved.name} runtime；请安装 `pip install 'ksadk[{resolved.name}]'`"
        )
    if resolved.version and installed != resolved.version:
        raise ManagedRuntimeError(
            f"本地 {resolved.name} runtime 版本为 {installed}，"
            f"配置要求 {resolved.version}；请安装匹配版本后重试"
        )
    validate_runtime_binary(resolved)
    return installed


def validate_runtime_binary(resolved: ResolvedRuntime) -> str:
    if resolved.name != "codex":
        return ""
    try:
        from codex_cli_bin import (  # type: ignore[import-not-found, import-untyped]
            bundled_codex_path,
        )

        codex_bin = bundled_codex_path()
        completed = subprocess.run(
            [str(codex_bin), "--version"],
            check=True,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (ImportError, OSError, subprocess.SubprocessError) as exc:
        raise ManagedRuntimeError(
            "本地 Codex 平台二进制不可用；请重新安装 `pip install --force-reinstall "
            "'ksadk[codex]'`"
        ) from exc
    output = f"{completed.stdout}\n{completed.stderr}".strip()
    if resolved.version and resolved.version not in output:
        raise ManagedRuntimeError(
            f"Codex CLI 版本输出为 {output or '(empty)'}，配置要求 {resolved.version}"
        )
    return output


async def resolve_local_managed_runtime(
    config: dict[str, Any],
    *,
    region: str,
) -> ResolvedRuntime:
    """Resolve and verify the native runtime used by ``ksadk web``."""
    name, requested_version = runtime_config(config)
    if requested_version:
        explicit_runtime = ResolvedRuntime(
            name=name,
            version=requested_version,
            source="manifest",
        )
        validate_installed_runtime(explicit_runtime)
        return explicit_runtime

    catalog_runtime: ResolvedRuntime | None = None
    try:
        catalog_runtime = await resolve_managed_runtime(config, region=region)
    except ManagedRuntimeError:
        pass
    if catalog_runtime is not None:
        validate_installed_runtime(catalog_runtime)
        return catalog_runtime

    installed = installed_runtime_version(name)
    if not installed:
        raise ManagedRuntimeError(
            f"本地缺少 {name} runtime；请安装 `pip install 'ksadk[{name}]'`"
        )
    resolved = ResolvedRuntime(
        name=name,
        version=installed,
        source="installed-unlocked",
    )
    validate_runtime_binary(resolved)
    return resolved

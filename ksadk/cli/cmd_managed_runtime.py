"""Production entrypoint for platform-owned declarative runtimes.

``agentengine managed-runtime`` is deliberately narrower than ``agentengine
web``: it accepts one Server-admitted ``agentengine.yaml`` mounted by the
control plane and never opens a browser or packages user code.  Runtime Service
uses this entrypoint for ``ArtifactType=ManagedRuntime`` workloads.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import click
import yaml

from ksadk.cli.cmd_web import web


def _load_managed_runtime_manifest(manifest_path: Path) -> dict[str, Any]:
    """Validate the small launch contract before starting a hosted process."""

    if manifest_path.name != "agentengine.yaml":
        raise click.ClickException("managed runtime manifest must be named agentengine.yaml")
    try:
        payload = yaml.safe_load(manifest_path.read_text(encoding="utf-8-sig"))
    except (OSError, yaml.YAMLError) as exc:
        raise click.ClickException(f"unable to read managed runtime manifest: {exc}") from exc
    if not isinstance(payload, dict):
        raise click.ClickException("managed runtime manifest must be a YAML object")
    if str(payload.get("artifact_type") or "").strip() != "ManagedRuntime":
        raise click.ClickException(
            "managed runtime manifest must declare artifact_type=ManagedRuntime"
        )
    framework = str(payload.get("framework") or "").strip().lower()
    runtime = payload.get("runtime")
    if not framework or not isinstance(runtime, dict):
        raise click.ClickException("managed runtime manifest requires framework and runtime")
    runtime_name = str(runtime.get("name") or "").strip().lower()
    runtime_version = str(runtime.get("version") or "").strip()
    if runtime_name != framework or not runtime_version:
        raise click.ClickException(
            "managed runtime manifest requires runtime.name=framework and runtime.version"
        )
    return payload


@click.command("managed-runtime", context_settings=dict(help_option_names=["-h", "--help"]))
@click.argument(
    "manifest_path",
    type=click.Path(exists=True, file_okay=True, dir_okay=False, path_type=Path),
)
@click.option("--port", type=int, default=8080, show_default=True)
@click.option("--host", default="0.0.0.0", show_default=True)
def managed_runtime(manifest_path: Path, port: int, host: str) -> None:
    """Serve one mounted, declarative ``agentengine.yaml`` in hosted mode."""

    manifest_path = manifest_path.resolve()
    _load_managed_runtime_manifest(manifest_path)
    # The command is a production process entrypoint, never a local UI action.
    # ``web`` owns the RuntimeAdapter composition, while no_open prevents an
    # accidental browser launch if this container is ever run with a display.
    os.environ["AGENTENGINE_MANAGED_RUNTIME"] = "1"
    web.callback(str(manifest_path.parent), port, host, None, True)


__all__ = ["managed_runtime"]

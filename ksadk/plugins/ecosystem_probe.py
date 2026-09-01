"""Read-only detection for supported plugin ecosystem manifests.

Detection examines only fixed manifest paths.  It never walks or imports a
package, executes install scripts, or parses DSH Cordis YAML (which may contain
JavaScript tags).  A directory with more than one ecosystem manifest remains
ambiguous until the caller selects an exact manifest reference.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from ksadk.plugins.ecosystem_bridge import (
    BridgeProbeExchange,
    BridgeProbeRequest,
    BridgeProbeResult,
    BridgeRejection,
    PluginManifestCandidate,
)

_MAX_MANIFEST_BYTES = 1024 * 1024


class EcosystemProbeError(ValueError):
    """A fixed manifest exists but cannot be inspected safely."""


def _read_manifest(path: Path, *, root: Path) -> bytes:
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise EcosystemProbeError("plugin manifest escaped the source root") from exc
    cursor = root
    for part in relative.parts:
        cursor /= part
        if cursor.is_symlink():
            raise EcosystemProbeError(
                f"plugin manifest path must not contain symlinks: {relative.as_posix()}"
            )
    if path.is_symlink():
        raise EcosystemProbeError(f"plugin manifest must not be a symlink: {path.name}")
    try:
        stat = path.stat()
    except OSError as exc:
        raise EcosystemProbeError(f"plugin manifest cannot be inspected: {path.name}") from exc
    if not path.is_file():
        raise EcosystemProbeError(f"plugin manifest is not a regular file: {path.name}")
    if stat.st_size > _MAX_MANIFEST_BYTES:
        raise EcosystemProbeError(
            f"plugin manifest exceeds {_MAX_MANIFEST_BYTES} bytes: {path.name}"
        )
    try:
        return path.read_bytes()
    except OSError as exc:
        raise EcosystemProbeError(f"plugin manifest cannot be read: {path.name}") from exc


def _json_object(raw: bytes, *, name: str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EcosystemProbeError(f"{name} must contain valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise EcosystemProbeError(f"{name} must contain one JSON object")
    return value


def _digest(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _candidate(
    *,
    ecosystem: str,
    integration_mode: str,
    maturity: str,
    manifest_kind: str,
    path: Path,
    raw: bytes,
) -> PluginManifestCandidate:
    return PluginManifestCandidate.model_validate(
        {
            "ecosystem": ecosystem,
            "integrationMode": integration_mode,
            "maturity": maturity,
            "manifestKind": manifest_kind,
            "manifestRef": path.resolve().as_uri(),
            "manifestDigest": _digest(raw),
        }
    )


def probe_ecosystem_manifests(
    root: Path,
    *,
    selected_manifest_ref: str | None = None,
) -> BridgeProbeExchange:
    """Detect the two supported Codex/DSH manifest formats without executing them."""

    if root.is_symlink():
        raise EcosystemProbeError("plugin source root must not be a symlink")
    if not root.is_dir():
        raise EcosystemProbeError("plugin source root must be an existing directory")
    root = root.resolve()
    candidates: list[PluginManifestCandidate] = []
    source_parts: list[tuple[str, bytes]] = []

    codex_path = root / ".codex-plugin" / "plugin.json"
    if codex_path.exists():
        raw = _read_manifest(codex_path, root=root)
        payload = _json_object(raw, name=".codex-plugin/plugin.json")
        if not isinstance(payload.get("name"), str) or not payload["name"]:
            raise EcosystemProbeError("Codex plugin manifest requires a non-empty name")
        candidates.append(
            _candidate(
                ecosystem="codex",
                integration_mode="bridged",
                maturity="detected",
                manifest_kind="codex-plugin",
                path=codex_path,
                raw=raw,
            )
        )
        source_parts.append((".codex-plugin/plugin.json", raw))

    package_path = root / "package.json"
    if package_path.exists():
        raw = _read_manifest(package_path, root=root)
        payload = _json_object(raw, name="package.json")
        dsh = payload.get("dsh")
        bundle = dsh.get("bundle") if isinstance(dsh, dict) else None
        patch = bundle.get("patch") if isinstance(bundle, dict) else None
        if isinstance(patch, str) and patch.strip():
            candidates.append(
                _candidate(
                    ecosystem="dsh",
                    integration_mode="linked",
                    maturity="experimental",
                    manifest_kind="dsh-bundle",
                    path=package_path,
                    raw=raw,
                )
            )
            source_parts.append(("package.json", raw))

    source_hash = hashlib.sha256()
    for relative_path, raw in sorted(source_parts):
        source_hash.update(relative_path.encode("utf-8"))
        source_hash.update(b"\0")
        source_hash.update(raw)
        source_hash.update(b"\0")
    source_digest = "sha256:" + source_hash.hexdigest()
    request = BridgeProbeRequest(
        source_ref=root.as_uri(),
        source_digest=source_digest,
        selected_manifest_ref=selected_manifest_ref,
    )

    if not candidates:
        result = BridgeProbeResult(
            candidates=(),
            selection_required=False,
            rejection=BridgeRejection(
                action="probe",
                code="unsupported",
                retryable=False,
                message="No supported plugin ecosystem manifest was detected.",
            ),
        )
    else:
        refs = {candidate.manifest_ref for candidate in candidates}
        if selected_manifest_ref is not None and selected_manifest_ref not in refs:
            raise EcosystemProbeError("selected manifest was not detected in the source directory")
        selection_required = len(candidates) > 1 and selected_manifest_ref is None
        selected = (
            None
            if selection_required
            else selected_manifest_ref or candidates[0].manifest_ref
        )
        result = BridgeProbeResult(
            candidates=tuple(candidates),
            selection_required=selection_required,
            selected_manifest_ref=selected,
        )
    return BridgeProbeExchange(request=request, result=result)


__all__ = ["EcosystemProbeError", "probe_ecosystem_manifests"]

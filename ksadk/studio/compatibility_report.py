"""Deterministic build-time compatibility report for AgentBundle v2.

The report contains only immutable, already-resolved facts.  It deliberately
does not inspect environment variables, import plugin entrypoints, or claim
that a process is healthy.  Runtime readiness remains an Activation concern.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from typing import Any

from ksadk.plugins.contracts import LockedCapability, PluginLockEntry, PluginManifest
from ksadk.plugins.resolver import (
    PluginResolutionError,
    ResolvedComposition,
    version_satisfies,
)
from ksadk.studio.contracts import AgentDraft

COMPATIBILITY_REPORT_FORMAT = "agentkit.compatibility-report/v1"
CURRENT_KERNEL_API_VERSION = "1.0.0"
CURRENT_RUNTIME_CONTRACT = "agentkit.runtime/v1"
SUPPORTED_RUNTIME_PROTOCOLS = (CURRENT_RUNTIME_CONTRACT,)


def compatibility_facts_digest(
    *,
    draft: AgentDraft,
    composition: ResolvedComposition | None,
    runtime_lock: Mapping[str, Any],
) -> str:
    """Address every static input that can change a compatibility conclusion."""

    payload: dict[str, Any] = {
        "allowedPermissions": sorted(set(draft.spec.security.allowed_permissions)),
        "runtime": {
            "type": str(runtime_lock.get("type") or ""),
            "version": str(runtime_lock.get("version") or ""),
        },
        "composition": None,
    }
    if composition is not None:
        payload["composition"] = {
            "profileDigest": composition.profile_digest,
            "pluginLockDigest": composition.plugin_lock_digest,
            "manifests": [
                manifest.model_dump(
                    by_alias=True,
                    exclude_none=True,
                    mode="json",
                )
                for manifest in sorted(
                    composition.manifests,
                    key=lambda item: (item.metadata.id, item.metadata.version),
                )
            ],
        }
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(canonical).hexdigest()}"


def build_bundle_compatibility_report(
    *,
    draft: AgentDraft,
    composition: ResolvedComposition | None,
    runtime_lock: Mapping[str, Any],
    resolved_digest: str,
    facts_digest: str,
    plugin_lock_digest: str,
    composition_profile_digest: str | None,
) -> dict[str, Any]:
    """Project auditable static compatibility without reading host secrets."""

    bundle = {
        "bundleFormat": "agentkit.bundle/v2",
        "compatibilityFactsDigest": facts_digest,
        "resolvedDigest": resolved_digest,
        "pluginLockDigest": plugin_lock_digest,
        **(
            {"compositionProfileDigest": composition_profile_digest}
            if composition_profile_digest is not None
            else {}
        ),
    }
    allowed_permissions = sorted(set(draft.spec.security.allowed_permissions))
    if composition is None:
        return _legacy_report(
            bundle=bundle,
            runtime_lock=runtime_lock,
            allowed_permissions=allowed_permissions,
        )

    manifests = {manifest.metadata.id: manifest for manifest in composition.manifests}
    locked = {entry.id: entry for entry in composition.plugin_lock.plugins}
    blockers: list[dict[str, str]] = []

    def block(code: str, subject: str, reason: str) -> None:
        blockers.append({"code": code, "subject": subject, "reason": reason})

    permission_items: list[dict[str, Any]] = []
    protocol_items: list[dict[str, Any]] = []
    kernel_items: list[dict[str, str]] = []
    capability_items: list[dict[str, str]] = []

    for entry in composition.plugin_lock.plugins:
        manifest = manifests.get(entry.id)
        if manifest is None:
            block(
                "plugin_manifest_facts_missing",
                entry.id,
                "The exact manifest used to resolve this lock entry is unavailable.",
            )
            permission_items.append(
                {
                    "pluginId": entry.id,
                    "required": [],
                    "unapproved": [],
                    "status": "blocked",
                }
            )
            protocol_items.append(
                {
                    "pluginId": entry.id,
                    "required": [],
                    "supported": list(SUPPORTED_RUNTIME_PROTOCOLS),
                    "status": "blocked",
                }
            )
            kernel_items.append(
                {
                    "pluginId": entry.id,
                    "required": "unknown",
                    "resolved": CURRENT_KERNEL_API_VERSION,
                    "status": "blocked",
                }
            )
            for capability in entry.provides:
                capability_items.append(
                    _capability_item(entry, capability, mode="unknown", status="blocked")
                )
            continue

        _append_manifest_conclusions(
            entry=entry,
            manifest=manifest,
            allowed_permissions=allowed_permissions,
            permission_items=permission_items,
            protocol_items=protocol_items,
            kernel_items=kernel_items,
            capability_items=capability_items,
            block=block,
        )

    provider_ref = composition.profile.agent_provider.ref
    provider_id, requested_version = _plugin_ref_parts(provider_ref)
    provider_entry = locked.get(provider_id)
    provider_manifest = manifests.get(provider_id)
    if provider_entry is None:
        block(
            "agent_provider_lock_missing",
            provider_id,
            "The CompositionProfile AgentProvider has no exact PluginLock entry.",
        )
    elif provider_entry.version != requested_version:
        block(
            "agent_provider_version_mismatch",
            provider_id,
            (
                f"Profile requests {requested_version}, but the lock resolves "
                f"{provider_entry.version}."
            ),
        )

    blockers.sort(key=lambda item: (item["code"], item["subject"], item["reason"]))
    blocked_subjects = {item["subject"] for item in blockers}
    overall = "blocked" if blockers else "compatible"
    provider_status = "blocked" if provider_id in blocked_subjects else "compatible"

    return {
        "format": COMPATIBILITY_REPORT_FORMAT,
        "scope": "build-time-static",
        "overallStatus": overall,
        "bundle": bundle,
        "host": {
            "kind": _host_kind(provider_manifest),
            "kernelApiVersion": CURRENT_KERNEL_API_VERSION,
            "runtimeContract": CURRENT_RUNTIME_CONTRACT,
            "readiness": "notEvaluated",
            "status": overall,
        },
        "provider": _provider_conclusion(
            provider_ref=provider_ref,
            provider_id=provider_id,
            requested_version=requested_version,
            entry=provider_entry,
            manifest=provider_manifest,
            status=provider_status,
        ),
        "kernelApi": sorted(kernel_items, key=lambda item: item["pluginId"]),
        "protocols": sorted(protocol_items, key=lambda item: item["pluginId"]),
        "permissions": {
            "allowed": allowed_permissions,
            "plugins": sorted(permission_items, key=lambda item: item["pluginId"]),
            "status": (
                "blocked"
                if any(item["status"] == "blocked" for item in permission_items)
                else "compatible"
            ),
        },
        "capabilities": sorted(
            capability_items,
            key=lambda item: (item["definition"], item["slot"], item["owner"]),
        ),
        "blockingReasons": blockers,
    }


def _legacy_report(
    *,
    bundle: dict[str, Any],
    runtime_lock: Mapping[str, Any],
    allowed_permissions: list[str],
) -> dict[str, Any]:
    runtime_type = str(runtime_lock.get("type") or "")
    version = str(runtime_lock.get("version") or "")
    return {
        "format": COMPATIBILITY_REPORT_FORMAT,
        "scope": "build-time-static",
        "overallStatus": "compatible",
        "bundle": bundle,
        "host": {
            "kind": "legacy-runtime",
            "kernelApiVersion": CURRENT_KERNEL_API_VERSION,
            "runtimeContract": CURRENT_RUNTIME_CONTRACT,
            "readiness": "notEvaluated",
            "status": "compatible",
        },
        "provider": {
            "ref": None,
            "id": runtime_type or "builtin.legacy",
            "hostRuntime": runtime_type or "unknown",
            "isolation": "legacy",
            "source": "builtin",
            "digest": None,
            "version": {
                "requested": version or None,
                "resolved": version or None,
                "status": "compatible" if version else "unknown",
            },
            "status": "compatible",
        },
        "kernelApi": [],
        "protocols": [
            {
                "pluginId": runtime_type or "builtin.legacy",
                "required": [CURRENT_RUNTIME_CONTRACT],
                "supported": list(SUPPORTED_RUNTIME_PROTOCOLS),
                "status": "compatible",
            }
        ],
        "permissions": {
            "allowed": allowed_permissions,
            "plugins": [],
            "status": "compatible",
        },
        "capabilities": [],
        "blockingReasons": [],
    }


def _append_manifest_conclusions(
    *,
    entry: PluginLockEntry,
    manifest: PluginManifest,
    allowed_permissions: list[str],
    permission_items: list[dict[str, Any]],
    protocol_items: list[dict[str, Any]],
    kernel_items: list[dict[str, str]],
    capability_items: list[dict[str, str]],
    block: Callable[[str, str, str], None],
) -> None:
    if (
        manifest.metadata.id != entry.id
        or manifest.metadata.version != entry.version
        or manifest.spec.provenance.digest != entry.digest
    ):
        block(
            "plugin_manifest_lock_mismatch",
            entry.id,
            "Resolved manifest identity or digest does not match the PluginLock entry.",
        )

    required_permissions = sorted(set(manifest.spec.permissions))
    unapproved = sorted(set(required_permissions) - set(allowed_permissions))
    permission_status = "blocked" if unapproved else "compatible"
    permission_items.append(
        {
            "pluginId": entry.id,
            "required": required_permissions,
            "unapproved": unapproved,
            "status": permission_status,
        }
    )
    if unapproved:
        block(
            "plugin_permission_unapproved",
            entry.id,
            "Unapproved permissions: " + ", ".join(unapproved),
        )

    protocols = sorted(set(manifest.spec.compatibility.runtime_protocols))
    protocol_status = (
        "compatible" if not protocols or CURRENT_RUNTIME_CONTRACT in protocols else "blocked"
    )
    protocol_items.append(
        {
            "pluginId": entry.id,
            "required": protocols,
            "supported": list(SUPPORTED_RUNTIME_PROTOCOLS),
            "status": protocol_status,
        }
    )
    if protocol_status == "blocked":
        block(
            "runtime_protocol_incompatible",
            entry.id,
            f"Plugin accepts {', '.join(protocols)}, host provides {CURRENT_RUNTIME_CONTRACT}.",
        )

    kernel_constraint = manifest.spec.compatibility.kernel_api
    try:
        kernel_compatible = version_satisfies(
            CURRENT_KERNEL_API_VERSION,
            kernel_constraint,
        )
    except PluginResolutionError:
        kernel_compatible = False
    kernel_items.append(
        {
            "pluginId": entry.id,
            "required": kernel_constraint,
            "resolved": CURRENT_KERNEL_API_VERSION,
            "status": "compatible" if kernel_compatible else "blocked",
        }
    )
    if not kernel_compatible:
        block(
            "kernel_api_incompatible",
            entry.id,
            f"Plugin requires {kernel_constraint}, host provides {CURRENT_KERNEL_API_VERSION}.",
        )

    offers = {(offer.definition, offer.slot): offer for offer in manifest.spec.provides}
    for capability in entry.provides:
        offer = offers.get((capability.definition, capability.slot))
        status = "compatible" if capability.owner == entry.id and offer is not None else "blocked"
        capability_items.append(
            _capability_item(
                entry,
                capability,
                mode=offer.mode if offer is not None else "unknown",
                status=status,
            )
        )
        if status == "blocked":
            block(
                "capability_lock_mismatch",
                entry.id,
                f"Locked capability {capability.definition} at {capability.slot} "
                "does not match the resolved manifest owner.",
            )


def _capability_item(
    entry: PluginLockEntry,
    capability: LockedCapability,
    *,
    mode: str,
    status: str,
) -> dict[str, str]:
    return {
        "definition": capability.definition,
        "slot": capability.slot,
        "owner": capability.owner,
        "version": entry.version,
        "mode": mode,
        "status": status,
    }


def _provider_conclusion(
    *,
    provider_ref: str,
    provider_id: str,
    requested_version: str,
    entry: PluginLockEntry | None,
    manifest: PluginManifest | None,
    status: str,
) -> dict[str, Any]:
    return {
        "ref": provider_ref,
        "id": provider_id,
        "hostRuntime": manifest.spec.runtime if manifest is not None else "unknown",
        "isolation": manifest.spec.isolation if manifest is not None else "unknown",
        "source": entry.source if entry is not None else "unknown",
        "digest": entry.digest if entry is not None else None,
        "version": {
            "requested": requested_version,
            "resolved": entry.version if entry is not None else None,
            "status": (
                "compatible"
                if entry is not None and entry.version == requested_version
                else "blocked"
            ),
        },
        "status": status,
    }


def _host_kind(provider: PluginManifest | None) -> str:
    if provider is not None and provider.spec.domain == "runtime-native":
        return "runtime-native"
    return "composition-host"


def _plugin_ref_parts(value: str) -> tuple[str, str]:
    plugin_id, version = value.removeprefix("plugin://").rsplit("@", 1)
    return plugin_id, version


__all__ = [
    "COMPATIBILITY_REPORT_FORMAT",
    "CURRENT_KERNEL_API_VERSION",
    "CURRENT_RUNTIME_CONTRACT",
    "build_bundle_compatibility_report",
    "compatibility_facts_digest",
]

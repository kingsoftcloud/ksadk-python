"""Frozen ``PluginEcosystemBridge/v1`` source contract.

An ecosystem bridge gives KsADK one lifecycle vocabulary while leaving Codex
and DeepSeek Harness in charge of their native
plugin ABI.  The contract is deliberately declarative: it does not import
third-party plugin code, run install scripts, translate Agent loops, or grant
permissions.  Inspection may report unknown versions and undeclared
permissions, but planning such an external transition fails closed.

The lifecycle is split into read-only discovery (``describe``/``probe``/
``inspect``), an immutable transition (``plan``/``stage``/``commit``), and
truthful observed state (``reconcile``/``rollback``/``dispose``).  External
plugins without an explicit permission declaration are rejected fail-closed.
Secrets may only cross the boundary as references.
"""

from __future__ import annotations

import re
from typing import Annotated, Literal, Protocol, cast, runtime_checkable
from urllib.parse import parse_qsl, urlsplit

from pydantic import AwareDatetime, ConfigDict, Field, RootModel, field_validator, model_validator

from ksadk.plugins.contracts import PluginContractModel

PluginEcosystem = Literal["codex", "dsh"]
PluginIntegrationMode = Literal["bridged", "linked"]
PluginSupportMaturity = Literal[
    "detected",
    "linked-ready",
    "bridged-ready",
    "unsupported",
    "experimental",
]
PluginDesiredState = Literal["disabled", "enabled"]
PluginObservedState = Literal[
    "resolved",
    "admitted",
    "staged",
    "starting",
    "ready",
    "degraded",
    "failed",
    "draining",
    "stopped",
    "disposed",
    "rejected",
]
BridgeAction = Literal[
    "describe",
    "probe",
    "inspect",
    "plan",
    "stage",
    "commit",
    "reconcile",
    "rollback",
    "dispose",
]

_ID = re.compile(r"^[a-z0-9]+(?:[._-][a-z0-9]+)*$")
_SEMVER = re.compile(r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_REF = re.compile(r"^[a-z][a-z0-9+.-]*://\S+$")
_SECRET_REF = re.compile(r"^(?:secret|env|credential|vault)://\S+$")
_PROTOCOL = re.compile(r"^[a-z][a-z0-9._-]*/v[1-9][0-9]*$")
_SENSITIVE_QUERY_KEY = re.compile(r"(?:secret|password|token|api[_-]?key)", re.IGNORECASE)


def _validate_id(value: str, *, field: str) -> str:
    if not _ID.fullmatch(value):
        raise ValueError(f"{field} must be a lowercase qualified id")
    return value


def _validate_semver(value: str, *, field: str) -> str:
    if not _SEMVER.fullmatch(value):
        raise ValueError(f"{field} must use an exact semantic version")
    return value


def _validate_digest(value: str) -> str:
    if not _DIGEST.fullmatch(value):
        raise ValueError("digest must be a lowercase sha256: digest")
    return value


def _validate_ref(value: str, *, field: str) -> str:
    if not _REF.fullmatch(value):
        raise ValueError(f"{field} must be an absolute typed reference")
    parsed = urlsplit(value)
    if parsed.username is not None or parsed.password is not None:
        raise ValueError(f"{field} must not embed credentials")
    if any(_SENSITIVE_QUERY_KEY.search(key) for key, _ in parse_qsl(parsed.query)):
        raise ValueError(f"{field} must not embed secret query parameters")
    return value


def _unique(value: tuple[str, ...], *, field: str) -> tuple[str, ...]:
    if any(not item for item in value):
        raise ValueError(f"{field} entries must not be empty")
    if len(value) != len(set(value)):
        raise ValueError(f"{field} entries must be unique")
    return value


class BridgeHostRequirement(PluginContractModel):
    host_id: str = Field(min_length=2, max_length=128)
    version_constraint: str = Field(min_length=1, max_length=128)
    protocol: str = Field(min_length=3, max_length=128)
    protocol_constraint: str = Field(min_length=1, max_length=128)

    @field_validator("host_id")
    @classmethod
    def validate_host_id(cls, value: str) -> str:
        return _validate_id(value, field="hostId")

    @field_validator("protocol")
    @classmethod
    def validate_protocol(cls, value: str) -> str:
        if not _PROTOCOL.fullmatch(value):
            raise ValueError("protocol must be a versioned protocol id")
        return value


class BridgeHostObservation(PluginContractModel):
    host_id: str = Field(min_length=2, max_length=128)
    available: bool
    version: str | None = None
    protocol: str | None = Field(default=None, min_length=3, max_length=128)
    protocol_version: str | None = None
    digest: str | None = None

    @field_validator("host_id")
    @classmethod
    def validate_host_id(cls, value: str) -> str:
        return _validate_id(value, field="hostId")

    @field_validator("version", "protocol_version")
    @classmethod
    def validate_optional_version(cls, value: str | None) -> str | None:
        if value is not None:
            return _validate_semver(value, field="host version")
        return value

    @field_validator("protocol")
    @classmethod
    def validate_optional_protocol(cls, value: str | None) -> str | None:
        if value is not None and not _PROTOCOL.fullmatch(value):
            raise ValueError("host protocol must be a versioned protocol id")
        return value

    @field_validator("digest")
    @classmethod
    def validate_optional_digest(cls, value: str | None) -> str | None:
        if value is not None:
            return _validate_digest(value)
        return value

    @model_validator(mode="after")
    def validate_observation(self) -> "BridgeHostObservation":
        trace = (self.version, self.protocol, self.protocol_version, self.digest)
        if self.available and any(item is None for item in trace):
            raise ValueError("available host observations require version, protocol, and digest")
        if not self.available and any(item is not None for item in trace):
            raise ValueError("unavailable host observations cannot claim host identity facts")
        return self


class BridgeDescriptor(PluginContractModel):
    descriptor_format: Literal["ksadk.plugin-ecosystem-bridge-descriptor/v1"] = (
        "ksadk.plugin-ecosystem-bridge-descriptor/v1"
    )
    bridge_id: str = Field(min_length=3, max_length=128)
    bridge_version: str
    bridge_digest: str
    ecosystem: PluginEcosystem
    integration_mode: PluginIntegrationMode
    maturity: PluginSupportMaturity
    host_requirement: BridgeHostRequirement | None
    supported_actions: tuple[BridgeAction, ...]

    @field_validator("bridge_id")
    @classmethod
    def validate_bridge_id(cls, value: str) -> str:
        return _validate_id(value, field="bridgeId")

    @field_validator("bridge_version")
    @classmethod
    def validate_bridge_version(cls, value: str) -> str:
        return _validate_semver(value, field="bridge version")

    @field_validator("bridge_digest")
    @classmethod
    def validate_bridge_digest(cls, value: str) -> str:
        return _validate_digest(value)

    @field_validator("supported_actions")
    @classmethod
    def validate_actions(cls, value: tuple[BridgeAction, ...]) -> tuple[BridgeAction, ...]:
        if not value or len(value) != len(set(value)):
            raise ValueError("supportedActions must be non-empty and unique")
        return value

    @model_validator(mode="after")
    def validate_mode(self) -> "BridgeDescriptor":
        if self.host_requirement is None:
            raise ValueError("bridged and linked bridges require an external host")
        if self.maturity == "bridged-ready" and self.integration_mode != "bridged":
            raise ValueError("bridged-ready maturity requires bridged integration")
        if self.maturity == "linked-ready" and self.integration_mode != "linked":
            raise ValueError("linked-ready maturity requires linked integration")
        return self


class BridgeDescribeRequest(PluginContractModel):
    request_format: Literal["ksadk.bridge-describe/v1"] = "ksadk.bridge-describe/v1"
    ecosystem: PluginEcosystem


class BridgeDescribeResult(PluginContractModel):
    result_format: Literal["ksadk.bridge-describe-result/v1"] = "ksadk.bridge-describe-result/v1"
    descriptor: BridgeDescriptor


class PluginManifestCandidate(PluginContractModel):
    ecosystem: PluginEcosystem
    integration_mode: PluginIntegrationMode
    maturity: PluginSupportMaturity
    manifest_kind: Literal["codex-plugin", "dsh-bundle"]
    manifest_ref: str = Field(min_length=4, max_length=4096)
    manifest_digest: str

    @field_validator("manifest_ref")
    @classmethod
    def validate_manifest_ref(cls, value: str) -> str:
        return _validate_ref(value, field="manifestRef")

    @field_validator("manifest_digest")
    @classmethod
    def validate_manifest_digest(cls, value: str) -> str:
        return _validate_digest(value)

    @model_validator(mode="after")
    def validate_candidate_mode(self) -> "PluginManifestCandidate":
        expected_kind = {"codex": "codex-plugin", "dsh": "dsh-bundle"}[
            self.ecosystem
        ]
        if self.manifest_kind != expected_kind:
            raise ValueError("manifestKind does not match the detected ecosystem")
        return self


class BridgeProbeRequest(PluginContractModel):
    request_format: Literal["ksadk.bridge-probe/v1"] = "ksadk.bridge-probe/v1"
    source_ref: str = Field(min_length=4, max_length=4096)
    source_digest: str
    selected_manifest_ref: str | None = Field(default=None, min_length=4, max_length=4096)

    @field_validator("source_ref", "selected_manifest_ref")
    @classmethod
    def validate_source_ref(cls, value: str | None) -> str | None:
        if value is not None:
            return _validate_ref(value, field="source reference")
        return value

    @field_validator("source_digest")
    @classmethod
    def validate_source_digest(cls, value: str) -> str:
        return _validate_digest(value)


class BridgeRejection(PluginContractModel):
    rejection_format: Literal["ksadk.bridge-rejection/v1"] = "ksadk.bridge-rejection/v1"
    action: BridgeAction
    code: Literal[
        "ambiguous_manifest",
        "permissions_undeclared",
        "host_unavailable",
        "host_incompatible",
        "digest_mismatch",
        "unsupported",
    ]
    retryable: bool
    message: str = Field(min_length=1, max_length=1024)
    host: BridgeHostObservation | None = None

    @model_validator(mode="after")
    def validate_host_rejection(self) -> "BridgeRejection":
        if self.code in {"host_unavailable", "host_incompatible"} and self.host is None:
            raise ValueError("host rejection requires a typed host observation")
        if self.code == "host_unavailable" and self.host is not None and self.host.available:
            raise ValueError("host_unavailable cannot claim that the host is available")
        return self


class BridgeProbeResult(PluginContractModel):
    result_format: Literal["ksadk.bridge-probe-result/v1"] = "ksadk.bridge-probe-result/v1"
    candidates: tuple[PluginManifestCandidate, ...] = ()
    selection_required: bool
    selected_manifest_ref: str | None = Field(default=None, min_length=4, max_length=4096)
    rejection: BridgeRejection | None = None

    @field_validator("selected_manifest_ref")
    @classmethod
    def validate_selected_manifest_ref(cls, value: str | None) -> str | None:
        if value is not None:
            return _validate_ref(value, field="selectedManifestRef")
        return value

    @model_validator(mode="after")
    def validate_candidates(self) -> "BridgeProbeResult":
        refs = [item.manifest_ref for item in self.candidates]
        if len(refs) != len(set(refs)):
            raise ValueError("probe candidates must have unique manifestRef values")
        if not refs:
            if self.rejection is None or self.rejection.action != "probe":
                raise ValueError("empty probe requires a typed probe rejection")
            if self.selection_required or self.selected_manifest_ref is not None:
                raise ValueError("rejected probe cannot claim a manifest selection")
            return self
        if self.rejection is not None:
            raise ValueError("successful probe candidates cannot carry a rejection")
        if self.selection_required and self.selected_manifest_ref is not None:
            raise ValueError("selectionRequired cannot also claim a selected manifest")
        if not self.selection_required:
            if self.selected_manifest_ref is None:
                raise ValueError("a completed probe requires selectedManifestRef")
            if self.selected_manifest_ref not in refs:
                raise ValueError("selectedManifestRef must identify a probe candidate")
        return self


class BridgeProbeExchange(PluginContractModel):
    fixture_kind: Literal["probe"] = "probe"
    request: BridgeProbeRequest
    result: BridgeProbeResult

    @model_validator(mode="after")
    def validate_explicit_selection(self) -> "BridgeProbeExchange":
        candidate_refs = {item.manifest_ref for item in self.result.candidates}
        if not candidate_refs:
            return self
        selected = self.request.selected_manifest_ref
        if len(candidate_refs) > 1 and selected is None:
            if not self.result.selection_required or self.result.selected_manifest_ref is not None:
                raise ValueError("multiple manifests require explicit selection")
        else:
            if selected is None:
                selected = next(iter(candidate_refs))
            if selected not in candidate_refs:
                raise ValueError("requested manifest selection was not detected")
            if self.result.selection_required or self.result.selected_manifest_ref != selected:
                raise ValueError("probe result must preserve the explicit manifest selection")
        return self


class EcosystemPluginDescriptor(PluginContractModel):
    descriptor_format: Literal["ksadk.ecosystem-plugin-descriptor/v1"] = (
        "ksadk.ecosystem-plugin-descriptor/v1"
    )
    ecosystem: PluginEcosystem
    plugin_id: str = Field(min_length=2, max_length=256)
    plugin_version: str | None = None
    integration_mode: PluginIntegrationMode
    maturity: PluginSupportMaturity
    source_ref: str = Field(min_length=4, max_length=4096)
    artifact_digest: str
    manifest_ref: str = Field(min_length=4, max_length=4096)
    manifest_digest: str
    host_requirement: BridgeHostRequirement | None
    permissions_declared: bool
    install_permissions: tuple[str, ...] = ()
    runtime_permissions: tuple[str, ...] = ()
    auth_scopes: tuple[str, ...] = ()
    secret_refs: tuple[str, ...] = ()
    components: tuple[str, ...] = ()

    @field_validator("plugin_id")
    @classmethod
    def validate_plugin_id(cls, value: str) -> str:
        return _validate_id(value, field="pluginId")

    @field_validator("plugin_version")
    @classmethod
    def validate_plugin_version(cls, value: str | None) -> str | None:
        if value is not None:
            return _validate_semver(value, field="plugin version")
        return value

    @field_validator("source_ref", "manifest_ref")
    @classmethod
    def validate_descriptor_ref(cls, value: str) -> str:
        return _validate_ref(value, field="descriptor reference")

    @field_validator("artifact_digest", "manifest_digest")
    @classmethod
    def validate_descriptor_digest(cls, value: str) -> str:
        return _validate_digest(value)

    @field_validator("install_permissions", "runtime_permissions", "auth_scopes", "components")
    @classmethod
    def validate_unique_values(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _unique(value, field="descriptor list")

    @field_validator("secret_refs")
    @classmethod
    def validate_secret_refs(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        _unique(value, field="secretRefs")
        if any(not _SECRET_REF.fullmatch(item) for item in value):
            raise ValueError("secretRefs may contain references only")
        return value

    @model_validator(mode="after")
    def validate_external_shape(self) -> "EcosystemPluginDescriptor":
        if self.integration_mode != "native" and self.host_requirement is None:
            raise ValueError("bridged and linked plugins require an external host")
        if self.integration_mode == "native" and self.host_requirement is not None:
            raise ValueError("native plugins cannot require an external host")
        return self


class BridgeInspectRequest(PluginContractModel):
    request_format: Literal["ksadk.bridge-inspect/v1"] = "ksadk.bridge-inspect/v1"
    bridge_id: str = Field(min_length=3, max_length=128)
    source_ref: str = Field(min_length=4, max_length=4096)
    source_digest: str
    candidate: PluginManifestCandidate

    @field_validator("bridge_id")
    @classmethod
    def validate_bridge_id(cls, value: str) -> str:
        return _validate_id(value, field="bridgeId")

    @field_validator("source_ref")
    @classmethod
    def validate_source_ref(cls, value: str) -> str:
        return _validate_ref(value, field="sourceRef")

    @field_validator("source_digest")
    @classmethod
    def validate_source_digest(cls, value: str) -> str:
        return _validate_digest(value)


class BridgeInspectResult(PluginContractModel):
    result_format: Literal["ksadk.bridge-inspect-result/v1"] = "ksadk.bridge-inspect-result/v1"
    status: Literal["accepted", "rejected"]
    descriptor: EcosystemPluginDescriptor | None = None
    rejection: BridgeRejection | None = None

    @model_validator(mode="after")
    def validate_outcome(self) -> "BridgeInspectResult":
        if self.status == "accepted" and (self.descriptor is None or self.rejection is not None):
            raise ValueError("accepted inspection requires descriptor only")
        if self.status == "rejected" and (self.rejection is None or self.descriptor is not None):
            raise ValueError("rejected inspection requires rejection only")
        return self


class BridgeInspectExchange(PluginContractModel):
    fixture_kind: Literal["rejection"] = "rejection"
    request: BridgeInspectRequest
    result: BridgeInspectResult


class BridgePlanRequest(PluginContractModel):
    request_format: Literal["ksadk.bridge-plan/v1"] = "ksadk.bridge-plan/v1"
    descriptor: EcosystemPluginDescriptor
    operation: Literal["install", "update", "enable", "disable", "uninstall"]
    desired_state: PluginDesiredState
    bound_references: tuple[str, ...] = ()
    authorization_ref: str = Field(min_length=4, max_length=2048)
    accept_undeclared_permissions: bool = False

    @field_validator("bound_references")
    @classmethod
    def validate_bound_references(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _unique(value, field="boundReferences")

    @field_validator("authorization_ref")
    @classmethod
    def validate_authorization_ref(cls, value: str) -> str:
        return _validate_ref(value, field="authorizationRef")

    @model_validator(mode="after")
    def validate_admission(self) -> "BridgePlanRequest":
        if self.descriptor.plugin_version is None:
            raise ValueError("planning requires an exact inspected plugin version")
        permissions_undeclared = (
            self.descriptor.integration_mode != "native"
            and not self.descriptor.permissions_declared
        )
        if permissions_undeclared and not self.accept_undeclared_permissions:
            raise ValueError(
                "external plugin permissions are undeclared; explicit risk acceptance is required"
            )
        if permissions_undeclared and (
            not self.descriptor.install_permissions
            or "process:host-user" not in self.descriptor.runtime_permissions
        ):
            raise ValueError(
                "undeclared external permissions require conservative install permissions "
                "and process:host-user runtime disclosure"
            )
        return self


class BridgeTransitionPlan(PluginContractModel):
    plan_format: Literal["ksadk.bridge-transition-plan/v1"] = "ksadk.bridge-transition-plan/v1"
    plan_id: str = Field(min_length=1, max_length=256)
    bridge_id: str = Field(min_length=3, max_length=128)
    bridge_version: str
    bridge_digest: str
    ecosystem: PluginEcosystem
    plugin_id: str = Field(min_length=2, max_length=256)
    plugin_version: str
    integration_mode: PluginIntegrationMode
    operation: Literal["install", "update", "enable", "disable", "uninstall"]
    desired_state: PluginDesiredState
    descriptor_digest: str
    artifact_digest: str
    manifest_digest: str
    install_permissions: tuple[str, ...] = ()
    runtime_permissions: tuple[str, ...] = ()
    auth_scopes: tuple[str, ...] = ()
    authorization_ref: str = Field(min_length=4, max_length=2048)
    permissions_declared: bool
    undeclared_permissions_accepted: bool = False
    bound_references: tuple[str, ...] = ()
    host_requirement: BridgeHostRequirement | None
    rollback_point_ref: str | None = Field(default=None, min_length=4, max_length=2048)
    rollback_point_digest: str | None = None
    plan_digest: str
    created_at: AwareDatetime

    @field_validator("bridge_id", "plugin_id")
    @classmethod
    def validate_ids(cls, value: str) -> str:
        return _validate_id(value, field="plan id")

    @field_validator("bridge_version", "plugin_version")
    @classmethod
    def validate_versions(cls, value: str) -> str:
        return _validate_semver(value, field="plan version")

    @field_validator(
        "bridge_digest",
        "descriptor_digest",
        "artifact_digest",
        "manifest_digest",
        "rollback_point_digest",
        "plan_digest",
    )
    @classmethod
    def validate_digests(cls, value: str | None) -> str | None:
        if value is not None:
            return _validate_digest(value)
        return value

    @field_validator("authorization_ref", "rollback_point_ref")
    @classmethod
    def validate_plan_refs(cls, value: str | None) -> str | None:
        if value is not None:
            return _validate_ref(value, field="plan reference")
        return value

    @field_validator(
        "install_permissions", "runtime_permissions", "auth_scopes", "bound_references"
    )
    @classmethod
    def validate_plan_lists(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _unique(value, field="plan list")

    @model_validator(mode="after")
    def validate_rollback_pair(self) -> "BridgeTransitionPlan":
        if (self.rollback_point_ref is None) != (self.rollback_point_digest is None):
            raise ValueError("rollback point reference and digest must appear together")
        if self.integration_mode != "native" and self.host_requirement is None:
            raise ValueError("external transition plans require a host")
        if self.permissions_declared and self.undeclared_permissions_accepted:
            raise ValueError(
                "undeclaredPermissionsAccepted must be false when permissions are declared"
            )
        if (
            self.integration_mode != "native"
            and not self.permissions_declared
            and not self.undeclared_permissions_accepted
        ):
            raise ValueError("external undeclared permissions require recorded risk acceptance")
        return self


class BridgePlanResult(PluginContractModel):
    result_format: Literal["ksadk.bridge-plan-result/v1"] = "ksadk.bridge-plan-result/v1"
    plan: BridgeTransitionPlan


class BridgeStageRequest(PluginContractModel):
    request_format: Literal["ksadk.bridge-stage/v1"] = "ksadk.bridge-stage/v1"
    plan: BridgeTransitionPlan
    expected_plan_digest: str

    @field_validator("expected_plan_digest")
    @classmethod
    def validate_expected_plan_digest(cls, value: str) -> str:
        return _validate_digest(value)

    @model_validator(mode="after")
    def validate_plan_digest(self) -> "BridgeStageRequest":
        if self.expected_plan_digest != self.plan.plan_digest:
            raise ValueError("expectedPlanDigest does not match the immutable plan")
        return self


class BridgeStageResult(PluginContractModel):
    result_format: Literal["ksadk.bridge-stage-result/v1"] = "ksadk.bridge-stage-result/v1"
    stage_id: str = Field(min_length=1, max_length=256)
    plan_id: str = Field(min_length=1, max_length=256)
    plan_digest: str
    bridge_version: str
    bridge_digest: str
    host: BridgeHostObservation
    artifact_digest: str
    manifest_digest: str
    native_stage_ref: str = Field(min_length=4, max_length=2048)
    native_stage_digest: str
    staged_at: AwareDatetime

    @field_validator(
        "plan_digest", "bridge_digest", "artifact_digest", "manifest_digest", "native_stage_digest"
    )
    @classmethod
    def validate_stage_digest(cls, value: str) -> str:
        return _validate_digest(value)

    @field_validator("bridge_version")
    @classmethod
    def validate_bridge_version(cls, value: str) -> str:
        return _validate_semver(value, field="bridge version")

    @field_validator("native_stage_ref")
    @classmethod
    def validate_native_stage_ref(cls, value: str) -> str:
        return _validate_ref(value, field="nativeStageRef")

    @model_validator(mode="after")
    def validate_host_available(self) -> "BridgeStageResult":
        if not self.host.available:
            raise ValueError("successful stage result requires an available host")
        return self


class BridgeCommitRequest(PluginContractModel):
    request_format: Literal["ksadk.bridge-commit/v1"] = "ksadk.bridge-commit/v1"
    stage_id: str = Field(min_length=1, max_length=256)
    plan_id: str = Field(min_length=1, max_length=256)
    plan_digest: str
    native_stage_ref: str = Field(min_length=4, max_length=2048)
    native_stage_digest: str
    idempotency_key: str = Field(min_length=8, max_length=256)

    @field_validator("plan_digest", "native_stage_digest")
    @classmethod
    def validate_commit_digest(cls, value: str) -> str:
        return _validate_digest(value)

    @field_validator("native_stage_ref")
    @classmethod
    def validate_native_stage_ref(cls, value: str) -> str:
        return _validate_ref(value, field="nativeStageRef")


class EcosystemInstallReceipt(PluginContractModel):
    receipt_format: Literal["ksadk.ecosystem-install-receipt/v1"] = (
        "ksadk.ecosystem-install-receipt/v1"
    )
    receipt_id: str = Field(min_length=1, max_length=256)
    plan_id: str = Field(min_length=1, max_length=256)
    plan_digest: str
    stage_id: str = Field(min_length=1, max_length=256)
    ecosystem: PluginEcosystem
    plugin_id: str = Field(min_length=2, max_length=256)
    plugin_version: str
    integration_mode: PluginIntegrationMode
    desired_state: PluginDesiredState
    bridge_id: str = Field(min_length=3, max_length=128)
    bridge_version: str
    bridge_digest: str
    host: BridgeHostObservation
    artifact_digest: str
    manifest_digest: str
    native_receipt_ref: str = Field(min_length=4, max_length=2048)
    native_receipt_digest: str
    bound_references: tuple[str, ...] = ()
    committed_at: AwareDatetime

    @field_validator("plugin_id", "bridge_id")
    @classmethod
    def validate_receipt_ids(cls, value: str) -> str:
        return _validate_id(value, field="receipt id")

    @field_validator("plugin_version", "bridge_version")
    @classmethod
    def validate_receipt_versions(cls, value: str) -> str:
        return _validate_semver(value, field="receipt version")

    @field_validator(
        "plan_digest",
        "bridge_digest",
        "artifact_digest",
        "manifest_digest",
        "native_receipt_digest",
    )
    @classmethod
    def validate_receipt_digests(cls, value: str) -> str:
        return _validate_digest(value)

    @field_validator("native_receipt_ref")
    @classmethod
    def validate_native_receipt_ref(cls, value: str) -> str:
        return _validate_ref(value, field="nativeReceiptRef")

    @field_validator("bound_references")
    @classmethod
    def validate_bound_references(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _unique(value, field="boundReferences")

    @model_validator(mode="after")
    def validate_host_available(self) -> "EcosystemInstallReceipt":
        if self.integration_mode != "native" and not self.host.available:
            raise ValueError("external install receipt requires an available native host")
        return self


class BridgeCommitResult(PluginContractModel):
    result_format: Literal["ksadk.bridge-commit-result/v1"] = "ksadk.bridge-commit-result/v1"
    receipt: EcosystemInstallReceipt


class BridgeReconcileRequest(PluginContractModel):
    request_format: Literal["ksadk.bridge-reconcile/v1"] = "ksadk.bridge-reconcile/v1"
    receipt_id: str = Field(min_length=1, max_length=256)
    native_receipt_ref: str = Field(min_length=4, max_length=2048)
    native_receipt_digest: str

    @field_validator("native_receipt_ref")
    @classmethod
    def validate_native_receipt_ref(cls, value: str) -> str:
        return _validate_ref(value, field="nativeReceiptRef")

    @field_validator("native_receipt_digest")
    @classmethod
    def validate_native_receipt_digest(cls, value: str) -> str:
        return _validate_digest(value)


class EcosystemPluginInventory(PluginContractModel):
    inventory_format: Literal["ksadk.ecosystem-inventory/v1"] = "ksadk.ecosystem-inventory/v1"
    receipt_id: str = Field(min_length=1, max_length=256)
    ecosystem: PluginEcosystem
    plugin_id: str = Field(min_length=2, max_length=256)
    plugin_version: str
    integration_mode: PluginIntegrationMode
    desired_state: PluginDesiredState
    observed_state: PluginObservedState
    maturity: PluginSupportMaturity
    bridge_id: str = Field(min_length=3, max_length=128)
    bridge_version: str
    bridge_digest: str
    host: BridgeHostObservation | None
    artifact_digest: str
    manifest_digest: str
    native_receipt_ref: str = Field(min_length=4, max_length=2048)
    native_receipt_digest: str
    bound_references: tuple[str, ...] = ()
    reason_code: str | None = Field(default=None, max_length=128)
    reconciled_at: AwareDatetime

    @field_validator("plugin_id", "bridge_id")
    @classmethod
    def validate_inventory_ids(cls, value: str) -> str:
        return _validate_id(value, field="inventory id")

    @field_validator("plugin_version", "bridge_version")
    @classmethod
    def validate_inventory_versions(cls, value: str) -> str:
        return _validate_semver(value, field="inventory version")

    @field_validator("bridge_digest", "artifact_digest", "manifest_digest", "native_receipt_digest")
    @classmethod
    def validate_inventory_digest(cls, value: str) -> str:
        return _validate_digest(value)

    @field_validator("native_receipt_ref")
    @classmethod
    def validate_native_receipt_ref(cls, value: str) -> str:
        return _validate_ref(value, field="nativeReceiptRef")

    @field_validator("bound_references")
    @classmethod
    def validate_bound_references(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _unique(value, field="boundReferences")

    @model_validator(mode="after")
    def validate_observed_truth(self) -> "EcosystemPluginInventory":
        if self.integration_mode != "native" and self.host is None:
            raise ValueError("external inventory requires a host observation")
        if self.host is not None and not self.host.available and self.observed_state == "ready":
            raise ValueError("unavailable host cannot be reconciled as ready")
        if self.observed_state in {"degraded", "failed", "rejected"} and not self.reason_code:
            raise ValueError("unhealthy inventory requires reasonCode")
        return self


class BridgeReconcileResult(PluginContractModel):
    result_format: Literal["ksadk.bridge-reconcile-result/v1"] = "ksadk.bridge-reconcile-result/v1"
    inventory: EcosystemPluginInventory


class BridgeRollbackRequest(PluginContractModel):
    request_format: Literal["ksadk.bridge-rollback/v1"] = "ksadk.bridge-rollback/v1"
    receipt_id: str = Field(min_length=1, max_length=256)
    rollback_point_ref: str = Field(min_length=4, max_length=2048)
    rollback_point_digest: str
    expected_native_receipt_digest: str

    @field_validator("rollback_point_ref")
    @classmethod
    def validate_rollback_point_ref(cls, value: str) -> str:
        return _validate_ref(value, field="rollbackPointRef")

    @field_validator("rollback_point_digest", "expected_native_receipt_digest")
    @classmethod
    def validate_rollback_digest(cls, value: str) -> str:
        return _validate_digest(value)


class BridgeRollbackResult(PluginContractModel):
    result_format: Literal["ksadk.bridge-rollback-result/v1"] = "ksadk.bridge-rollback-result/v1"
    receipt_id: str = Field(min_length=1, max_length=256)
    state: Literal["rolled-back"]
    native_receipt_ref: str = Field(min_length=4, max_length=2048)
    native_receipt_digest: str
    rolled_back_at: AwareDatetime

    @field_validator("native_receipt_ref")
    @classmethod
    def validate_native_receipt_ref(cls, value: str) -> str:
        return _validate_ref(value, field="nativeReceiptRef")

    @field_validator("native_receipt_digest")
    @classmethod
    def validate_native_receipt_digest(cls, value: str) -> str:
        return _validate_digest(value)


class BridgeDisposeRequest(PluginContractModel):
    request_format: Literal["ksadk.bridge-dispose/v1"] = "ksadk.bridge-dispose/v1"
    receipt_id: str = Field(min_length=1, max_length=256)
    native_receipt_ref: str = Field(min_length=4, max_length=2048)
    native_receipt_digest: str

    @field_validator("native_receipt_ref")
    @classmethod
    def validate_native_receipt_ref(cls, value: str) -> str:
        return _validate_ref(value, field="nativeReceiptRef")

    @field_validator("native_receipt_digest")
    @classmethod
    def validate_native_receipt_digest(cls, value: str) -> str:
        return _validate_digest(value)


class BridgeDisposeResult(PluginContractModel):
    result_format: Literal["ksadk.bridge-dispose-result/v1"] = "ksadk.bridge-dispose-result/v1"
    receipt_id: str = Field(min_length=1, max_length=256)
    state: Literal["disposed"]
    native_receipt_ref: str = Field(min_length=4, max_length=2048)
    native_receipt_digest: str
    disposed_at: AwareDatetime

    @field_validator("native_receipt_ref")
    @classmethod
    def validate_native_receipt_ref(cls, value: str) -> str:
        return _validate_ref(value, field="nativeReceiptRef")

    @field_validator("native_receipt_digest")
    @classmethod
    def validate_native_receipt_digest(cls, value: str) -> str:
        return _validate_digest(value)


class PluginEcosystemBridgeTranscript(PluginContractModel):
    fixture_kind: Literal["lifecycle"] = "lifecycle"
    contract_format: Literal["ksadk.plugin-ecosystem-bridge/v1"] = (
        "ksadk.plugin-ecosystem-bridge/v1"
    )
    describe_request: BridgeDescribeRequest
    describe_result: BridgeDescribeResult
    probe: BridgeProbeExchange
    inspect_request: BridgeInspectRequest
    inspect_result: BridgeInspectResult
    plan_request: BridgePlanRequest
    plan_result: BridgePlanResult
    stage_request: BridgeStageRequest
    stage_result: BridgeStageResult
    commit_request: BridgeCommitRequest
    commit_result: BridgeCommitResult
    reconcile_request: BridgeReconcileRequest
    reconcile_result: BridgeReconcileResult
    rollback_request: BridgeRollbackRequest
    rollback_result: BridgeRollbackResult
    dispose_request: BridgeDisposeRequest
    dispose_result: BridgeDisposeResult

    @model_validator(mode="after")
    def validate_traceability(self) -> "PluginEcosystemBridgeTranscript":
        bridge = self.describe_result.descriptor
        descriptor = self.inspect_result.descriptor
        if descriptor is None:
            raise ValueError("lifecycle transcript requires an accepted inspection")
        plan = self.plan_result.plan
        receipt = self.commit_result.receipt
        inventory = self.reconcile_result.inventory
        if self.describe_request.ecosystem != bridge.ecosystem:
            raise ValueError("describe result ecosystem does not match request")
        if descriptor.ecosystem != bridge.ecosystem or plan.ecosystem != bridge.ecosystem:
            raise ValueError("ecosystem identity must be preserved through planning")
        if self.plan_request.descriptor != descriptor:
            raise ValueError("plan request must preserve the inspected plugin descriptor")
        if (plan.bridge_id, plan.bridge_version, plan.bridge_digest) != (
            bridge.bridge_id,
            bridge.bridge_version,
            bridge.bridge_digest,
        ):
            raise ValueError("transition plan must pin the described bridge identity")
        if descriptor.host_requirement != bridge.host_requirement:
            raise ValueError("plugin descriptor must preserve the bridge host requirement")
        if plan.host_requirement != bridge.host_requirement:
            raise ValueError("transition plan must preserve the bridge host requirement")
        if (
            plan.permissions_declared != descriptor.permissions_declared
            or plan.undeclared_permissions_accepted
            != self.plan_request.accept_undeclared_permissions
        ):
            raise ValueError("transition plan must preserve permission disclosure and acceptance")
        if (
            plan.plugin_id,
            plan.plugin_version,
            plan.integration_mode,
            plan.desired_state,
            plan.bound_references,
        ) != (
            descriptor.plugin_id,
            descriptor.plugin_version,
            descriptor.integration_mode,
            self.plan_request.desired_state,
            self.plan_request.bound_references,
        ):
            raise ValueError("transition plan must preserve plugin identity and desired state")
        expected_digests = (
            descriptor.artifact_digest,
            descriptor.manifest_digest,
        )
        observed_digest_pairs = (
            (plan.artifact_digest, plan.manifest_digest),
            (self.stage_result.artifact_digest, self.stage_result.manifest_digest),
            (receipt.artifact_digest, receipt.manifest_digest),
            (inventory.artifact_digest, inventory.manifest_digest),
        )
        if any(pair != expected_digests for pair in observed_digest_pairs):
            raise ValueError("artifact and manifest digests must remain traceable")
        if (
            self.stage_result.plan_id != plan.plan_id
            or self.stage_result.plan_digest != plan.plan_digest
        ):
            raise ValueError("stage result must trace to the immutable transition plan")
        if self.commit_request.stage_id != self.stage_result.stage_id:
            raise ValueError("commit request must trace to the staged transition")
        if receipt.plan_id != plan.plan_id or receipt.stage_id != self.stage_result.stage_id:
            raise ValueError("install receipt must trace to plan and stage")
        if self.reconcile_request.receipt_id != receipt.receipt_id:
            raise ValueError("reconcile request must trace to the install receipt")
        if inventory.receipt_id != receipt.receipt_id:
            raise ValueError("inventory must trace to the install receipt")
        host_observations = (self.stage_result.host, receipt.host, inventory.host)
        if inventory.host is None or any(
            item != self.stage_result.host for item in host_observations
        ):
            raise ValueError("native host version, protocol, and digest must remain traceable")
        if self.rollback_request.receipt_id != receipt.receipt_id:
            raise ValueError("rollback request must trace to the install receipt")
        if self.dispose_request.receipt_id != receipt.receipt_id:
            raise ValueError("dispose request must trace to the install receipt")
        native_digests = {
            receipt.native_receipt_digest,
            self.reconcile_request.native_receipt_digest,
            inventory.native_receipt_digest,
            self.rollback_request.expected_native_receipt_digest,
        }
        if len(native_digests) != 1:
            raise ValueError("native host receipt digest must remain traceable")
        if (
            self.dispose_request.native_receipt_ref != self.rollback_result.native_receipt_ref
            or self.dispose_request.native_receipt_digest
            != self.rollback_result.native_receipt_digest
        ):
            raise ValueError("dispose request must consume the rollback receipt")
        if (
            self.dispose_result.native_receipt_ref != self.dispose_request.native_receipt_ref
            or self.dispose_result.native_receipt_digest
            != self.dispose_request.native_receipt_digest
        ):
            raise ValueError("dispose result must preserve the disposed native receipt")
        return self


BridgeFixturePayload = Annotated[
    PluginEcosystemBridgeTranscript | BridgeProbeExchange | BridgeInspectExchange,
    Field(discriminator="fixture_kind"),
]


class PluginEcosystemBridgeFixture(RootModel[BridgeFixturePayload]):
    """Schema root shared by lifecycle, ambiguous-manifest and rejection goldens."""

    model_config = ConfigDict(frozen=True)


def ecosystem_bridge_json_schema() -> dict[str, object]:
    """Return the canonical JSON Schema for all v1 bridge conformance fixtures."""

    schema = cast(dict[str, object], PluginEcosystemBridgeFixture.model_json_schema(by_alias=True))
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = "https://ksadk.local/contracts/plugin/v1/plugin-ecosystem-bridge.schema.json"
    schema["title"] = "PluginEcosystemBridge/v1 conformance fixtures"
    return schema


@runtime_checkable
class PluginEcosystemBridge(Protocol):
    """Execution-neutral bridge SPI; implementations live outside this contract."""

    async def describe(self, request: BridgeDescribeRequest) -> BridgeDescribeResult: ...

    async def probe(self, request: BridgeProbeRequest) -> BridgeProbeResult: ...

    async def inspect(self, request: BridgeInspectRequest) -> BridgeInspectResult: ...

    async def plan(self, request: BridgePlanRequest) -> BridgePlanResult: ...

    async def stage(self, request: BridgeStageRequest) -> BridgeStageResult: ...

    async def commit(self, request: BridgeCommitRequest) -> BridgeCommitResult: ...

    async def reconcile(self, request: BridgeReconcileRequest) -> BridgeReconcileResult: ...

    async def rollback(self, request: BridgeRollbackRequest) -> BridgeRollbackResult: ...

    async def dispose(self, request: BridgeDisposeRequest) -> BridgeDisposeResult: ...


__all__ = [
    "BridgeAction",
    "BridgeCommitRequest",
    "BridgeCommitResult",
    "BridgeDescribeRequest",
    "BridgeDescribeResult",
    "BridgeDescriptor",
    "BridgeDisposeRequest",
    "BridgeDisposeResult",
    "BridgeHostObservation",
    "BridgeHostRequirement",
    "BridgeInspectExchange",
    "BridgeInspectRequest",
    "BridgeInspectResult",
    "BridgePlanRequest",
    "BridgePlanResult",
    "BridgeProbeExchange",
    "BridgeProbeRequest",
    "BridgeProbeResult",
    "BridgeReconcileRequest",
    "BridgeReconcileResult",
    "BridgeRejection",
    "BridgeRollbackRequest",
    "BridgeRollbackResult",
    "BridgeStageRequest",
    "BridgeStageResult",
    "BridgeTransitionPlan",
    "EcosystemInstallReceipt",
    "EcosystemPluginDescriptor",
    "EcosystemPluginInventory",
    "PluginDesiredState",
    "PluginEcosystem",
    "PluginEcosystemBridge",
    "PluginEcosystemBridgeFixture",
    "PluginEcosystemBridgeTranscript",
    "PluginIntegrationMode",
    "PluginManifestCandidate",
    "PluginObservedState",
    "PluginSupportMaturity",
    "ecosystem_bridge_json_schema",
]

"""Strict, additive Teams cloud/node contracts shared by trusted adapters.

These DTOs validate wire data, not authority. A parsed execution reference or
permit is never proof of authorization; hosts must verify its signature and
resolve the persisted command before using it. Existing local Teams inputs and
the AgentControl canonical command/digest remain unchanged.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime
from pathlib import PurePosixPath
from typing import Annotated, Any, Literal

import rfc8785
from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    TypeAdapter,
    model_validator,
)

from ksadk.kernel.contracts import AgentControlCommand, AgentControlPermit

API_VERSION = "teams.ksadk.io/v1"
NODE_VERSION = "teams-node/v1"
HOST_VERSION = "teams-host/v1"
JS_MAX_INTEGER = 2**53 - 1
MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_MATERIAL_BYTES = 64 * 1024 * 1024
MAX_REPORT_BYTES = 1024 * 1024

Identifier = Annotated[str, Field(strict=True, min_length=1, max_length=256, pattern=r"^\S+$")]
OperationKey = Annotated[str, Field(strict=True, min_length=1, max_length=200)]
Digest = Annotated[str, Field(strict=True, pattern=r"^sha256:[0-9a-f]{64}$")]
CommandId = Annotated[
    str,
    Field(strict=True, pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"),
]
Sequence = Annotated[int, Field(strict=True, ge=0, le=JS_MAX_INTEGER)]
Revision = Annotated[int, Field(strict=True, ge=1, le=JS_MAX_INTEGER)]


def _utc_millis(value: str) -> str:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z", value):
        raise ValueError("expected UTC RFC3339 with millisecond precision")
    datetime.fromisoformat(value.replace("Z", "+00:00"))
    return value


Timestamp = Annotated[str, Field(strict=True), AfterValidator(_utc_millis)]


def canonical_bytes(value: Any) -> bytes:
    """RFC 8785 Teams JSON; never substitute this for legacy AgentControl hashing.

    JCS uses UTF-16 object-key order and ECMAScript number serialization. It
    rejects non-finite numbers, lone surrogates and integers outside the exact
    interoperable range instead of silently changing signed input.
    """
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    _validate_canonical_input(value)
    return rfc8785.dumps(value)


def _validate_canonical_input(value: Any, depth: int = 0) -> None:
    # JavaScript cannot distinguish 1e20 from an integer parsed from JSON.
    # Apply the same safe-integer boundary even to integral Python floats.
    if depth > 50:
        raise ValueError("Teams JSON nesting exceeds 50 levels")
    if isinstance(value, float) and value.is_integer() and abs(value) > JS_MAX_INTEGER:
        raise ValueError("integer-valued JSON number exceeds the safe integer range")
    if isinstance(value, dict):
        for item in value.values():
            _validate_canonical_input(item, depth + 1)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _validate_canonical_input(item, depth + 1)


def digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_bytes(value)).hexdigest()


class WireModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, validate_default=True, revalidate_instances="always"
    )


class NodeTarget(WireModel):
    kind: Literal["node"]
    nodeId: Identifier
    nodeGeneration: Revision


class CloudTarget(WireModel):
    kind: Literal["cloud_agent"]
    agentId: Identifier
    versionId: Identifier
    runtimeId: Identifier
    agentInstanceId: Identifier


ExecutionTarget = Annotated[NodeTarget | CloudTarget, Field(discriminator="kind")]


class TeamsExecutionRef(WireModel):
    authorityId: Identifier
    groupId: Identifier
    teamRunId: Identifier
    memberId: Identifier
    runMemberId: Identifier
    taskId: Identifier | None = None
    attemptId: Identifier
    deliveryId: Identifier
    bindingRef: Identifier
    providerRef: Identifier
    sessionId: Identifier
    commandId: CommandId
    idempotencyKey: OperationKey
    schedulerEpoch: Revision
    leaderEpoch: Revision
    dispatchEpoch: Revision
    attemptEpoch: Revision
    target: ExecutionTarget
    bundleDigest: Digest
    contractDigest: Digest
    capabilitiesDigest: Digest
    nativeRunId: Identifier | None = None


class TerminalEvidence(WireModel):
    sessionId: Identifier
    runId: Identifier
    commandId: CommandId
    terminalSeq: Sequence
    terminalEventDigest: Digest
    resultDigest: Digest | None = None


class HostReceipt(WireModel):
    status: Literal["missing", "accepted", "duplicate", "rejected", "uncertain"]
    commandId: CommandId
    idempotencyKey: OperationKey
    payloadDigest: Digest
    storeIncarnation: Identifier
    runId: Identifier | None = None
    acceptedSeq: Sequence | None = None
    nativeStatus: (
        Literal[
            "queued",
            "running",
            "waiting",
            "awaiting_approval",
            "waiting_for_node",
            "succeeded",
            "failed",
            "cancelled",
            "interrupted",
        ]
        | None
    ) = None
    terminalEvidence: TerminalEvidence | None = None

    @model_validator(mode="after")
    def validate_evidence(self) -> HostReceipt:
        if self.status == "missing" and any(
            x is not None
            for x in (self.runId, self.acceptedSeq, self.nativeStatus, self.terminalEvidence)
        ):
            raise ValueError("missing receipt cannot contain execution evidence")
        evidence = self.terminalEvidence
        if evidence is not None:
            if self.commandId != evidence.commandId or self.runId != evidence.runId:
                raise ValueError("terminal evidence does not match original command/run")
            if self.nativeStatus not in {"succeeded", "failed", "cancelled", "interrupted"}:
                raise ValueError("terminal evidence requires a canonical terminal status")
        return self


class EventSourceRef(WireModel):
    nativeRunId: Identifier | None = None
    parentRunId: Identifier | None = None
    nativeEventType: Identifier | None = None


class CanonicalExecutionEvent(WireModel):
    eventId: Identifier
    sessionId: Identifier
    runId: Identifier
    seq: Sequence
    family: Identifier
    type: Identifier
    payload: dict[str, JsonValue]
    sourceRef: EventSourceRef


class CanonicalEventBatch(WireModel):
    sessionId: Identifier
    storeIncarnation: Identifier
    afterSeq: Sequence
    nextSeq: Sequence
    snapshotUpperSeq: Sequence
    hasMore: bool = Field(strict=True)
    items: list[CanonicalExecutionEvent] = Field(max_length=200)

    @model_validator(mode="after")
    def validate_cursor(self) -> CanonicalEventBatch:
        if not self.afterSeq <= self.nextSeq <= self.snapshotUpperSeq:
            raise ValueError("invalid canonical session cursor interval")
        if self.hasMore != (self.nextSeq < self.snapshotUpperSeq):
            raise ValueError("hasMore must reflect the frozen session high watermark")
        if self.hasMore and self.nextSeq == self.afterSeq:
            raise ValueError("nonfinal event page must advance the scan cursor")
        previous = self.afterSeq
        event_ids: set[str] = set()
        for item in self.items:
            if item.sessionId != self.sessionId or not previous < item.seq <= self.nextSeq:
                raise ValueError("events must be ordered within the declared session interval")
            if item.eventId in event_ids:
                raise ValueError("duplicate event in one canonical page")
            previous = item.seq
            event_ids.add(item.eventId)
        if len(canonical_bytes(self)) > MAX_REPORT_BYTES:
            raise ValueError("canonical event page exceeds one MiB")
        return self


class ExecutionAuthorization(WireModel):
    permitKind: Literal["execute", "recovery"]
    permit: str = Field(min_length=1, max_length=16384)
    grantRevision: Revision | None = None
    # Server-signed native transport authorization may rotate without changing
    # the frozen logical operation. The Host compares canonical command identity
    # and independently verifies this native permit before any new admission.
    nativePermit: AgentControlPermit | None = None
    nativeCommand: AgentControlCommand | None = None


class PreparePayload(WireModel):
    contextRef: Identifier
    contextDigest: Digest
    materialManifestRef: Identifier | None = None


class SubmitPayload(WireModel):
    # Native AgentControl envelope retains its exact existing serialization.
    # Host admission validates it with the original Kernel command model.
    command: dict[str, JsonValue]
    payloadDigest: Digest

    @model_validator(mode="after")
    def validate_native_command(self) -> SubmitPayload:
        from ksadk.kernel.contracts import AgentControlCommand

        command = AgentControlCommand.model_validate(self.command)
        if command.command_type != "enqueue":
            raise ValueError("submit requires the original enqueue command")
        for key in ("execution_grant_id", "execution_policy_ref", "teams_context_ref"):
            if not isinstance(command.payload.get(key), str) or not command.payload[key]:
                raise ValueError(f"Teams enqueue requires {key}")
        return self


class LookupPayload(WireModel):
    commandId: CommandId
    idempotencyKey: OperationKey
    payloadDigest: Digest
    storeIncarnation: Identifier


class SetGrantPayload(WireModel):
    grantId: Identifier
    expectedRevision: Revision
    state: Literal["active", "suspended", "revoked"]
    attemptEpoch: Revision
    expiresAt: Timestamp
    renewalId: OperationKey | None = None
    controlId: OperationKey | None = None

    @model_validator(mode="after")
    def validate_operation_identity(self) -> SetGrantPayload:
        if (self.renewalId is None) == (self.controlId is None):
            raise ValueError("exactly one renewalId or controlId is required")
        if self.renewalId is not None and self.state != "active":
            raise ValueError("renewal cannot change grant state")
        return self


class GetGrantPayload(WireModel):
    grantId: Identifier
    renewalId: OperationKey | None = None


class SetAdmissionPayload(WireModel):
    grantId: Identifier
    expectedAdmissionRevision: Revision
    admissionAllowed: bool = Field(strict=True)
    attemptEpoch: Revision
    controlId: OperationKey


class CancelPayload(WireModel):
    controlCommandId: CommandId
    controlIdempotencyKey: OperationKey
    targetRunId: Identifier
    reason: str = Field(max_length=2000)


class RespondInteractionPayload(WireModel):
    controlCommandId: CommandId
    controlIdempotencyKey: OperationKey
    interactionId: Identifier
    expectedRevision: Revision
    action: Literal["approve", "reject", "submit", "cancel"]
    response: dict[str, JsonValue]


class ObservePayload(WireModel):
    sessionId: Identifier
    afterSeq: Sequence
    limit: int = Field(default=200, strict=True, ge=1, le=200)


class NodeCommandBase(WireModel):
    protocolVersion: Literal["teams-node/v1"] = NODE_VERSION
    nodeCommandId: CommandId
    operationKey: OperationKey
    ref: TeamsExecutionRef
    commandDigest: Digest
    issuedAt: Timestamp
    claimLeaseUntil: Timestamp
    authorization: ExecutionAuthorization

    @model_validator(mode="after")
    def validate_node_target(self) -> NodeCommandBase:
        if self.ref.target.kind != "node":
            raise ValueError("node transport requires a node target")
        if self.nodeCommandId == self.ref.commandId:
            raise ValueError("node operation ID must differ from original enqueue ID")
        if self.claimLeaseUntil <= self.issuedAt:
            raise ValueError("claim lease must end after issue time")
        return self


class PrepareCommand(NodeCommandBase):
    operation: Literal["prepare"]
    lane: Literal["execution"]
    payload: PreparePayload


class SubmitCommand(NodeCommandBase):
    operation: Literal["submit"]
    lane: Literal["execution"]
    payload: SubmitPayload

    @model_validator(mode="after")
    def validate_original_enqueue(self) -> SubmitCommand:
        command = self.payload.command
        if (
            command["command_id"] != self.ref.commandId
            or command["idempotency_key"] != self.ref.idempotencyKey
            or command["session_id"] != self.ref.sessionId
        ):
            raise ValueError("enqueue differs from frozen execution reference")
        return self


class LookupCommand(NodeCommandBase):
    operation: Literal["lookup"]
    lane: Literal["control"]
    payload: LookupPayload

    @model_validator(mode="after")
    def validate_original_lookup(self) -> LookupCommand:
        if (
            self.payload.commandId != self.ref.commandId
            or self.payload.idempotencyKey != self.ref.idempotencyKey
        ):
            raise ValueError("lookup must target the original enqueue")
        return self


class SetGrantCommand(NodeCommandBase):
    operation: Literal["set_grant"]
    lane: Literal["control"]
    payload: SetGrantPayload


class GetGrantCommand(NodeCommandBase):
    operation: Literal["get_grant"]
    lane: Literal["control"]
    payload: GetGrantPayload


class SetAdmissionCommand(NodeCommandBase):
    operation: Literal["set_admission"]
    lane: Literal["control"]
    payload: SetAdmissionPayload

    @model_validator(mode="after")
    def validate_admission_attempt(self) -> SetAdmissionCommand:
        if self.payload.attemptEpoch != self.ref.attemptEpoch:
            raise ValueError("admission must target the exact attempt epoch")
        if self.payload.controlId == self.ref.idempotencyKey:
            raise ValueError("admission control must not reuse the enqueue key")
        return self


class CancelCommand(NodeCommandBase):
    operation: Literal["cancel"]
    lane: Literal["control"]
    payload: CancelPayload

    @model_validator(mode="after")
    def validate_control_identity(self) -> CancelCommand:
        if self.payload.controlCommandId in {self.nodeCommandId, self.ref.commandId}:
            raise ValueError("cancel requires an independent native control identity")
        if self.payload.controlIdempotencyKey == self.ref.idempotencyKey:
            raise ValueError("cancel must not reuse the enqueue key")
        if self.ref.nativeRunId is None or self.payload.targetRunId != self.ref.nativeRunId:
            raise ValueError("cancel must target the exact original native run")
        return self


class RespondInteractionCommand(NodeCommandBase):
    operation: Literal["respond_interaction"]
    lane: Literal["control"]
    payload: RespondInteractionPayload

    @model_validator(mode="after")
    def validate_control_identity(self) -> RespondInteractionCommand:
        if self.payload.controlCommandId in {self.nodeCommandId, self.ref.commandId}:
            raise ValueError("interaction requires an independent native control identity")
        if (
            self.payload.controlIdempotencyKey == self.ref.idempotencyKey
            or self.ref.nativeRunId is None
        ):
            raise ValueError("interaction needs original native run and its own key")
        return self


class ObserveCommand(NodeCommandBase):
    operation: Literal["observe"]
    lane: Literal["control"]
    payload: ObservePayload

    @model_validator(mode="after")
    def validate_session(self) -> ObserveCommand:
        if self.payload.sessionId != self.ref.sessionId:
            raise ValueError("observation must target the original session")
        return self


NodeCommand = Annotated[
    PrepareCommand
    | SubmitCommand
    | LookupCommand
    | SetGrantCommand
    | SetAdmissionCommand
    | GetGrantCommand
    | CancelCommand
    | RespondInteractionCommand
    | ObserveCommand,
    Field(discriminator="operation"),
]
NODE_COMMAND_ADAPTER = TypeAdapter(NodeCommand)


def node_command_digest(command: NodeCommandBase) -> str:
    """Exclude renewable transport authority, preserve immutable operation."""
    ref = command.ref.model_dump(mode="json")
    # A new scheduler may redeliver the same operation under a new lease.
    ref.pop("schedulerEpoch")
    return digest(
        {
            "protocolVersion": command.protocolVersion,
            "nodeCommandId": command.nodeCommandId,
            "operationKey": command.operationKey,
            "operation": command.operation,
            "lane": command.lane,
            "ref": ref,
            "payload": command.payload.model_dump(mode="json"),
        }
    )


def parse_node_command(value: Any) -> NodeCommandBase:
    command = NODE_COMMAND_ADAPTER.validate_python(value)
    if node_command_digest(command) != command.commandDigest:
        raise ValueError("node command digest mismatch")
    execute = command.operation in {"prepare", "submit", "respond_interaction", "set_admission"}
    execute |= command.operation == "set_grant" and command.payload.state != "revoked"
    if execute and command.authorization.permitKind != "execute":
        raise ValueError("recovery authorization cannot grant execution")
    return command


class ProbeAuthorization(WireModel):
    permitKind: Literal["probe"]
    permit: str = Field(min_length=1, max_length=16384)


class NodeProbeCommand(WireModel):
    protocolVersion: Literal["teams-node/v1"] = NODE_VERSION
    nodeCommandId: CommandId
    operation: Literal["describe"]
    lane: Literal["control"] = "control"
    operationKey: OperationKey
    authorityId: Identifier
    nodeId: Identifier
    bindingRef: Identifier
    localBindingRef: Identifier
    expectedDigests: dict[Literal["bundle", "contract", "capabilities"], Digest] = Field(
        min_length=3, max_length=3
    )
    nodeGeneration: Revision
    claimLeaseUntil: Timestamp
    authorization: ProbeAuthorization
    commandDigest: Digest


class NodeProbeResult(WireModel):
    bindingRef: Identifier
    localBindingRef: Identifier
    agentInstanceId: Identifier
    storeIncarnation: Identifier
    capabilities: dict[str, JsonValue]
    capabilitiesDigest: Digest
    bundleDigest: Digest
    contractDigest: Digest

    @model_validator(mode="after")
    def validate_capabilities(self):
        if self.capabilitiesDigest != digest(self.capabilities):
            raise ValueError("probe capability digest mismatch")
        return self


def node_probe_digest(command: NodeProbeCommand) -> str:
    return digest(
        command.model_dump(
            mode="json",
            exclude={
                "authorization",
                "claimLeaseUntil",
                "commandDigest",
            },
        )
    )


def parse_node_message(value: Any) -> NodeCommandBase | NodeProbeCommand:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    if isinstance(value, dict) and value.get("operation") == "describe":
        parsed = NodeProbeCommand.model_validate(value)
        if node_probe_digest(parsed) != parsed.commandDigest:
            raise ValueError("node probe digest mismatch")
        return parsed
    return parse_node_command(value)


class MaterialProof(WireModel):
    manifestRef: Identifier
    digest: Digest


class ReportError(WireModel):
    code: Identifier
    retryable: bool = Field(strict=True)


class NodeProbeReport(WireModel):
    reportKind: Literal["probe"] = "probe"
    nodeCommandId: CommandId
    nodeGeneration: Revision
    commandDigest: Digest
    resultRevision: Revision
    phase: Literal["described", "uncertain", "rejected"]
    probeResult: NodeProbeResult | None = None
    error: ReportError | None = None

    @model_validator(mode="after")
    def validate_described(self):
        if (self.phase == "described") != (self.probeResult is not None):
            raise ValueError("described probe requires the exact Host result")
        return self


class ExecutionGrantView(WireModel):
    grantId: Identifier
    state: Literal["active", "suspended", "revoked"]
    revision: Revision
    attemptEpoch: Revision
    expiresAt: Timestamp
    admissionAllowed: bool = Field(strict=True)
    admissionRevision: Revision


class GrantCommandView(WireModel):
    messageId: Identifier
    commandId: CommandId
    idempotencyKey: OperationKey
    inboxState: Literal["accepted", "claimed", "completed", "discarded"]
    runId: Identifier | None = None
    runState: (
        Literal[
            "pending",
            "running",
            "paused",
            "waiting",
            "completed",
            "failed",
            "cancelled",
            "interrupted",
        ]
        | None
    ) = None


class ExecutionGrantBarrierView(WireModel):
    queuedMessageIds: list[Identifier] = Field(default_factory=list, max_length=10000)
    inFlightMessageIds: list[Identifier] = Field(default_factory=list, max_length=10000)
    discardedMessageIds: list[Identifier] = Field(default_factory=list, max_length=10000)
    settledMessageIds: list[Identifier] = Field(default_factory=list, max_length=10000)
    commands: list[GrantCommandView] = Field(default_factory=list, max_length=10000)


class ExecutionGrantSnapshot(WireModel):
    storeIncarnation: Identifier
    grant: ExecutionGrantView
    barrier: ExecutionGrantBarrierView


class GrantMutationReceipt(WireModel):
    operationId: OperationKey
    status: Literal["applied"] = "applied"
    snapshot: ExecutionGrantSnapshot


class PrepareResult(WireModel):
    operation: Literal["prepare"]
    contextRef: Identifier
    contextDigest: Digest
    snapshot: ExecutionGrantSnapshot


class SetGrantResult(WireModel):
    operation: Literal["set_grant"]
    mutationReceipt: GrantMutationReceipt


class SetAdmissionResult(WireModel):
    operation: Literal["set_admission"]
    mutationReceipt: GrantMutationReceipt


class GetGrantResult(WireModel):
    operation: Literal["get_grant"]
    storeIncarnation: Identifier
    current: ExecutionGrantSnapshot | None = None
    lookupOperationId: OperationKey | None = None
    mutationReceipt: GrantMutationReceipt | None = None

    @model_validator(mode="after")
    def validate_lookup(self) -> GetGrantResult:
        if self.current and self.current.storeIncarnation != self.storeIncarnation:
            raise ValueError("grant snapshot belongs to another store")
        if self.mutationReceipt is not None:
            if (
                self.mutationReceipt.operationId != self.lookupOperationId
                or self.mutationReceipt.snapshot.storeIncarnation != self.storeIncarnation
            ):
                raise ValueError("grant mutation receipt differs from requested operation/store")
        return self


HostOperationResult = Annotated[
    PrepareResult | SetGrantResult | GetGrantResult | SetAdmissionResult,
    Field(discriminator="operation"),
]
HOST_OPERATION_RESULT_ADAPTER = TypeAdapter(HostOperationResult)


class ExecutionResultCandidate(WireModel):
    result: str = Field(max_length=MAX_REPORT_BYTES)
    artifacts: list[dict[str, JsonValue]] = Field(default_factory=list, max_length=1000)


class ExecutionResultUsage(WireModel):
    totalTokens: Sequence


class ExecutionResult(WireModel):
    """One canonical completion, persisted atomically with its Node report.

    terminalEvidence.resultDigest covers the exact candidate. completionDigest
    independently covers status, candidate, usage and terminal evidence together;
    it cannot silently change accounting or terminal identity during a retry.
    """

    status: Literal["succeeded", "failed", "cancelled", "interrupted"]
    candidate: ExecutionResultCandidate
    usage: ExecutionResultUsage
    terminalEvidence: TerminalEvidence
    completionDigest: Digest

    @model_validator(mode="after")
    def validate_completion(self):
        if self.terminalEvidence.resultDigest != digest(self.candidate):
            raise ValueError("canonical candidate digest mismatch")
        value = self.model_dump(mode="json", exclude={"completionDigest"})
        if self.completionDigest != digest(value):
            raise ValueError("canonical completion digest mismatch")
        if len(canonical_bytes(self)) > MAX_REPORT_BYTES:
            raise ValueError("canonical completion exceeds one MiB")
        return self

    @classmethod
    def from_host_result(cls, value):
        facts = {key: value[key] for key in ("status", "candidate", "usage", "terminalEvidence")}
        if isinstance(facts["terminalEvidence"], BaseModel):
            facts["terminalEvidence"] = facts["terminalEvidence"].model_dump(mode="json")
        return cls.model_validate(facts | {"completionDigest": digest(facts)})


class NodeReport(WireModel):
    nodeCommandId: CommandId
    nodeGeneration: Revision
    commandDigest: Digest
    resultRevision: Revision
    phase: Literal[
        "prepared",
        "submitted",
        "running",
        "waiting",
        "terminal",
        "uncertain",
        "rejected",
        "control_applied",
    ]
    receipt: HostReceipt | None = None
    eventBatch: CanonicalEventBatch | None = None
    materialProof: MaterialProof | None = None
    error: ReportError | None = None
    operationResult: HostOperationResult | None = None
    executionResult: ExecutionResult | None = None

    @model_validator(mode="after")
    def validate_report(self) -> NodeReport:
        if self.phase == "prepared" and not isinstance(self.operationResult, PrepareResult):
            raise ValueError("prepared report requires durable preparation result")
        if self.phase == "control_applied" and not isinstance(
            self.operationResult, (SetGrantResult, SetAdmissionResult, GetGrantResult)
        ):
            raise ValueError("control report requires typed operation result")
        if self.operationResult is not None and self.phase not in {"prepared", "control_applied"}:
            raise ValueError("operation result is only valid in a preparation/control report")
        if self.phase == "terminal" and (
            self.receipt is None or self.receipt.terminalEvidence is None
        ):
            raise ValueError("terminal report requires canonical terminal evidence")
        if self.phase == "terminal":
            if self.executionResult is None:
                raise ValueError("terminal report requires the complete canonical result")
            if (
                self.executionResult.terminalEvidence != self.receipt.terminalEvidence
                or self.executionResult.status != self.receipt.nativeStatus
            ):
                raise ValueError("completion differs from original receipt terminal")
        elif self.executionResult is not None:
            raise ValueError("completion is only valid in a terminal report")
        if len(canonical_bytes(self)) > MAX_REPORT_BYTES:
            raise ValueError("node report exceeds one MiB")
        return self


def validate_relative_path(value: str) -> str:
    path = PurePosixPath(value)
    if (
        not value
        or len(value) > 4096
        or path.is_absolute()
        or "\\" in value
        or any(ord(c) < 32 or ord(c) == 127 for c in value)
        or any(part in {"", ".", ".."} for part in value.split("/"))
        or re.match(r"^[A-Za-z]:", value)
        or str(path) != value
    ):
        raise ValueError("material path must be a normalized relative file path")
    return value


RelativePath = Annotated[str, Field(strict=True), AfterValidator(validate_relative_path)]


class MaterialEntry(WireModel):
    path: RelativePath
    digest: Digest
    sizeBytes: int = Field(strict=True, ge=0, le=MAX_FILE_BYTES)
    mediaType: str = Field(strict=True, min_length=1, max_length=200)


class MaterialManifest(WireModel):
    kind: Literal["files", "git_snapshot"]
    sourceCommit: Annotated[str, Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")] | None = None
    entries: list[MaterialEntry] = Field(min_length=1, max_length=4096)

    @model_validator(mode="after")
    def validate_manifest(self) -> MaterialManifest:
        if (self.kind == "git_snapshot") != (self.sourceCommit is not None):
            raise ValueError("only a git snapshot requires a full source commit")
        if sum(entry.sizeBytes for entry in self.entries) > MAX_MATERIAL_BYTES:
            raise ValueError("material manifest exceeds 64 MiB")
        paths = [entry.path for entry in self.entries]
        if len(paths) != len(set(paths)):
            raise ValueError("duplicate material path")
        path_set = set(paths)
        if any(str(parent) in path_set for path in paths for parent in PurePosixPath(path).parents):
            raise ValueError("material file cannot also be a parent directory")
        return self

    @property
    def manifest_digest(self) -> str:
        value = self.model_dump(mode="json", exclude_none=True)
        value["entries"].sort(key=lambda entry: entry["path"])
        return digest(value)


class MaterialCreateInput(MaterialManifest):
    idempotencyKey: OperationKey

    @property
    def manifest_digest(self) -> str:
        # Operation identity is not part of content identity.
        value = self.model_dump(mode="json", exclude={"idempotencyKey"})
        return MaterialManifest.model_validate(value).manifest_digest


CONTRACT_MODELS: dict[str, Any] = {
    "execution-reference": TeamsExecutionRef,
    "host-receipt": HostReceipt,
    "canonical-event-batch": CanonicalEventBatch,
    "node-command": NODE_COMMAND_ADAPTER,
    "node-probe-command": NodeProbeCommand,
    "node-report": NodeReport,
    "node-probe-report": NodeProbeReport,
    "host-operation-result": HOST_OPERATION_RESULT_ADAPTER,
    "execution-result": ExecutionResult,
    "material-manifest": MaterialManifest,
    "material-create": MaterialCreateInput,
}


def contract_models() -> dict[str, Any]:
    """All frozen wire models, imported lazily to keep runtime DTOs cycle-free."""
    from .build_artifacts import LoadedBuildEvidence, TeamsBuildManifest
    from .effect_contracts import (
        EffectOutcome,
        EffectPreparedReceipt,
        EffectProjection,
        EffectProjectionPage,
        EffectReconcileInput,
        EffectRecord,
        EffectReportReceipt,
        EffectRequest,
        EffectsRequest,
        ExecutionEffectsPage,
    )
    from .release_contracts import (
        BuildArtifactLookupInput,
        BuildArtifactReceipt,
        DeploymentArtifactReceipt,
        MissingBuildArtifact,
        RecordedBuildArtifact,
        ReleaseVerificationReceipt,
        ReleaseVerifyInput,
    )

    return CONTRACT_MODELS | {
        "effect-request": EffectRequest,
        "effect-outcome": EffectOutcome,
        "effect-prepared-receipt": EffectPreparedReceipt,
        "effect-record": EffectRecord,
        "effect-report-receipt": EffectReportReceipt,
        "effect-reconcile-input": EffectReconcileInput,
        "effect-projection": EffectProjection,
        "effect-projection-page": EffectProjectionPage,
        "execution-effects-request": EffectsRequest,
        "execution-effects-page": ExecutionEffectsPage,
        "build-manifest": TeamsBuildManifest,
        "loaded-build-evidence": LoadedBuildEvidence,
        "build-artifact-receipt": BuildArtifactReceipt,
        "build-artifact-lookup-input": BuildArtifactLookupInput,
        "build-artifact-lookup-result": TypeAdapter(MissingBuildArtifact | RecordedBuildArtifact),
        "deployment-artifact-receipt": DeploymentArtifactReceipt,
        "release-verify-input": ReleaseVerifyInput,
        "release-verification-receipt": ReleaseVerificationReceipt,
    }


def contract_versions() -> dict[str, str]:
    from .effect_contracts import EFFECTS_PORT_VERSION
    from .release_contracts import BUILD_VERSION, LOADED_BUILD_VERSION

    return {
        "apiVersion": API_VERSION,
        "nodeVersion": NODE_VERSION,
        "hostVersion": HOST_VERSION,
        "effectsPortVersion": EFFECTS_PORT_VERSION,
        "buildVersion": BUILD_VERSION,
        "loadedBuildVersion": LOADED_BUILD_VERSION,
    }


def contract_schemas() -> dict[str, dict[str, Any]]:
    return {
        name: model.json_schema() if isinstance(model, TypeAdapter) else model.model_json_schema()
        for name, model in contract_models().items()
    }

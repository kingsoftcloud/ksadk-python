"""Reject missing disclosure of a host-known empty capability inventory.

This checks one fact from the locked Build, not arbitrary answer semantics or
whether a resource exists online. The unchanged Native verification/repair
protocol handles a missing statement; no extra tools or retries are granted.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from ksadk.harness.native.completion import (
    AcceptanceItem,
    HostResolvedAnswer,
    TaskContract,
    VerificationItemResult,
    VerificationRepairTarget,
    VerificationReport,
)
from ksadk.harness.native.ports import VerificationBundle, VerificationPort, VerificationRequest
from ksadk.harness.native.state import RunScope
from ksadk.studio.native_final_shape import EVIDENCE_VERIFIER_REF, EVIDENCE_VERIFIER_VERSION

VERIFIER_REF = "studio.locked-empty-inventory-disclosure"
VERIFIER_VERSION = "1"
ACCEPTANCE_ID = "locked_inventory_disclosure"


def _digest(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode()).hexdigest()


def _has_plain_statement(text: str, statement: str) -> bool:
    fence = None
    for line in text.splitlines():
        stripped = line.strip()
        marker = next((prefix for prefix in ("```", "~~~") if stripped.startswith(prefix)), None)
        if marker is not None:
            if fence is None:
                fence = marker
            elif fence == marker:
                fence = None
            continue
        if fence is None and not line.startswith(("    ", "\t")) and stripped == statement:
            return True
    return False


@dataclass(frozen=True)
class EmptyInventoryDisclosure:
    """Constructed by the host after resolving the immutable Build bindings."""

    capability_label: str

    def __post_init__(self):
        if self.capability_label not in {"Skill", "MCP", "工具"}:
            raise ValueError("unsupported inventory label")

    @property
    def statement(self) -> str:
        return f"当前 Agent 没有绑定 {self.capability_label}。"

    @property
    def description(self) -> str:
        return json.dumps(
            {"authority": "locked_agent_build", "required_statement": self.statement},
            ensure_ascii=False,
            sort_keys=True,
        )

    def extend(self, contract: TaskContract) -> TaskContract:
        item = AcceptanceItem(
            acceptance_id=ACCEPTANCE_ID,
            verifier_ref=VERIFIER_REF,
            verifier_version=VERIFIER_VERSION,
            verification_method="deterministic",
            description=self.description,
        )
        return contract.model_copy(
            update={
                "revision": f"{contract.revision}+inventory-{_digest(self.description)[7:23]}",
                "items": (*contract.items, item),
            }
        )

    def resolve(self, contract: TaskContract, goal: str) -> TaskContract:
        # Custom action/output acceptance belongs to the normal Agent loop.
        # Resolve only the plain informational goal plus this one known fact.
        if (
            contract.allowed_tool_names != ()
            or contract.final_submission_schema is not None
            or {item.acceptance_id for item in contract.items}
            != {"current_user_goal", ACCEPTANCE_ID}
        ):
            return contract
        goal_item = next(i for i in contract.items if i.acceptance_id == "current_user_goal")
        identity = (
            goal_item.verifier_ref, goal_item.verifier_version, goal_item.verification_method
        )
        if identity not in {
            ("studio.independent-evidence-unavailable", "1", "human"),
            (EVIDENCE_VERIFIER_REF, EVIDENCE_VERIFIER_VERSION, "deterministic"),
        }:
            # Resolve only the host's unverified baseline. A deterministic
            # semantic extension still needs its own execution and evidence.
            return contract
        return TaskContract.model_validate(
            contract.model_dump(mode="json")
            | {
                "host_resolved_answer": HostResolvedAnswer(
                    goal_digest=hashlib.sha256(goal.encode()).hexdigest(),
                    goal_revision=0,
                    text=self.statement,
                    authority="locked_agent_build",
                    evidence_ref=_digest(self.description),
                ).model_dump(mode="json"),
            }
        )


class StudioInventoryDisclosureVerifier:
    """An additive fact gate; all other goals delegate without modification."""

    def __init__(
        self,
        *,
        scope: RunScope,
        baseline: VerificationPort,
        disclosure: EmptyInventoryDisclosure | None,
    ):
        self.scope = scope.model_copy(deep=True)
        self.baseline = baseline
        self.disclosure = disclosure

    async def verify(self, request: VerificationRequest) -> VerificationBundle:
        item = next(
            (item for item in request.contract.items if item.acceptance_id == ACCEPTANCE_ID),
            None,
        )
        if item is None:
            return await self.baseline.verify(request)
        if request.scope != self.scope:
            raise ValueError("native_inventory_scope_mismatch")
        if (
            self.disclosure is None
            or item.verifier_ref != VERIFIER_REF
            or item.verifier_version != VERIFIER_VERSION
            or item.verification_method != "deterministic"
            or item.description != self.disclosure.description
        ):
            raise ValueError("native_inventory_contract_mismatch")
        # The baseline sees its original contract. Its results remain evidence
        # for the original acceptance items; the composition binds both here.
        baseline_contract = request.contract.model_copy(
            update={"items": tuple(i for i in request.contract.items if i != item)}
        )
        bundle = await self.baseline.verify(
            request.model_copy(update={"contract": baseline_contract})
        )
        results = (
            list(bundle.report.items)
            if bundle.report is not None
            else [
                VerificationItemResult(
                    acceptance_id=i.acceptance_id,
                    status="not_run",
                    verifier_id=i.verifier_ref,
                    verifier_version=i.verifier_version,
                    verification_method=i.verification_method,
                    evidence_refs=(),
                    safe_summary="No independent semantic verifier is available.",
                )
                for i in baseline_contract.items
            ]
        )
        # Require the host-supplied fact as a plain answer statement. A quoted
        # rule, JSON field, inline code, or promise is not this disclosure.
        is_final = request.purpose == "final"
        present = is_final and _has_plain_statement(request.final_text, self.disclosure.statement)
        missing_final = is_final and not present
        result = VerificationItemResult(
            acceptance_id=item.acceptance_id,
            status="passed" if present else "failed" if is_final else "not_run",
            verifier_id=VERIFIER_REF,
            verifier_version=VERIFIER_VERSION,
            verification_method="deterministic",
            evidence_refs=(_digest(self.disclosure.description + "\0" + request.final_text),),
            safe_summary=(
                "The locked Build inventory statement is present. "
                "Broader semantics remain unverified."
                if present
                else "Final inventory disclosure has not been submitted."
                if not is_final
                else "Answer using this known fact from the locked Agent Build on a plain line: "
                + self.disclosure.statement
                + " Workspace or network inspection cannot change the binding."
            ),
            repairable=missing_final,
            repair_target=VerificationRepairTarget(kind="final_output") if missing_final else None,
        )
        results.append(result)
        candidate = bundle.candidate
        report = VerificationReport(
            candidate_id=candidate.candidate_id,
            contract_revision=request.contract.revision,
            subject_digest=candidate.subject_digest,
            subject_context_digest=candidate.subject_context_digest,
            environment_digest=candidate.environment_digest,
            items=tuple(results),
            repairable=(missing_final or bool(bundle.report and bundle.report.repairable)),
            failure_fingerprint=(
                _digest(f"{VERIFIER_REF}\0{request.contract.revision}\0missing-disclosure")
                if missing_final
                else bundle.report.failure_fingerprint
                if bundle.report
                else ""
            ),
        )
        return VerificationBundle(
            candidate=candidate, report=report, post_workspace=bundle.post_workspace
        )

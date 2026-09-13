"""长任务的确定性预算、停滞、验收与交付结果控制。

控制器不替模型规划，也不相信模型自报“已完成”。它只消费可回放事实：
模型/工具/Usage/Artifact 事件、固化验收项和单调预算。其状态可写入 Graph
Checkpoint，恢复后继续累计，避免通过重启绕过预算或停滞阈值。
"""

from __future__ import annotations

import hashlib
import json
import math
import time
from collections import Counter, deque
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ksadk._str_enum import StrEnum
from ksadk.harness.events import EventType, RuntimeEvent


class ControlAction(StrEnum):
    CONTINUE = "continue"
    REPLAN = "replan"
    CLOSE = "close"
    STOP = "stop"


class RunOutcome(StrEnum):
    VERIFIED = "verified"
    PARTIAL = "partial"
    UNVERIFIED = "unverified"
    BLOCKED = "blocked"
    BUDGET_EXHAUSTED = "budget_exhausted"
    FAILED = "failed"


class ExperimentDecision(StrEnum):
    KEEP = "keep"
    ROLLBACK = "rollback"


class RunControlStop(RuntimeError):
    """Internal control-flow signal for a deterministic, non-generic stop."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class RunControlReplan(RuntimeError):
    """Tool-loop signal asking the model to choose a materially different path."""

    def __init__(self, reason: str) -> None:
        super().__init__(f"run controller requires replanning: {reason}")
        self.reason = reason


class AcceptanceCheck(BaseModel):
    """Deterministic acceptance check; model confidence is intentionally absent."""

    model_config = ConfigDict(extra="forbid")

    check_id: str = Field(min_length=2, max_length=96, pattern=r"^[a-zA-Z0-9._-]+$")
    kind: str = Field(
        pattern=r"^(text_contains|event_exists|tool_succeeded|artifact_exists)$"
    )
    expected: str = Field(min_length=1, max_length=1024)
    required: bool = True


class RunLimits(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_wall_seconds: float | None = Field(default=None, gt=0)
    max_model_calls: int | None = Field(default=None, ge=1)
    max_tool_calls: int | None = Field(default=None, ge=0)
    max_total_tokens: int | None = Field(default=None, ge=1)
    soft_limit_ratio: float = Field(default=0.85, gt=0, lt=1)

    @model_validator(mode="after")
    def require_a_hard_limit(self) -> "RunLimits":
        if all(
            value is None
            for value in (
                self.max_wall_seconds,
                self.max_model_calls,
                self.max_tool_calls,
                self.max_total_tokens,
            )
        ):
            raise ValueError("run control requires at least one hard limit")
        return self


class StagnationPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action_repeat_threshold: int = Field(default=3, ge=2, le=20)
    error_repeat_threshold: int = Field(default=2, ge=2, le=20)
    max_replans: int = Field(default=2, ge=0, le=10)
    fingerprint_window: int = Field(default=12, ge=2, le=100)


class MilestoneSpec(BaseModel):
    """A controller-owned milestone verified only by acceptance latches."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    milestone_id: str = Field(min_length=2, max_length=96, pattern=r"^[a-zA-Z0-9._-]+$")
    description: str = Field(min_length=1, max_length=2048)
    acceptance_ids: tuple[str, ...] = Field(min_length=1)
    depends_on: tuple[str, ...] = ()


class RunControlSpec(BaseModel):
    """Build/run creation time contract; immutable to the executing Agent."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    objective: str = Field(min_length=1, max_length=8192)
    acceptance: tuple[AcceptanceCheck, ...] = ()
    milestones: tuple[MilestoneSpec, ...] = ()
    limits: RunLimits
    stagnation: StagnationPolicy = Field(default_factory=StagnationPolicy)

    @model_validator(mode="after")
    def unique_acceptance_ids(self) -> "RunControlSpec":
        ids = [item.check_id for item in self.acceptance]
        if len(ids) != len(set(ids)):
            raise ValueError("acceptance check_id values must be unique")
        milestone_ids = [item.milestone_id for item in self.milestones]
        if len(milestone_ids) != len(set(milestone_ids)):
            raise ValueError("milestone_id values must be unique")
        known_checks = set(ids)
        known_milestones = set(milestone_ids)
        for milestone in self.milestones:
            unknown_checks = set(milestone.acceptance_ids) - known_checks
            unknown_dependencies = set(milestone.depends_on) - known_milestones
            if unknown_checks:
                raise ValueError(
                    f"milestone {milestone.milestone_id!r} references unknown acceptance: "
                    f"{sorted(unknown_checks)}"
                )
            if unknown_dependencies or milestone.milestone_id in milestone.depends_on:
                raise ValueError(
                    f"milestone {milestone.milestone_id!r} has invalid dependencies"
                )
        _assert_acyclic_milestones(self.milestones)
        return self

    @property
    def digest(self) -> str:
        raw = json.dumps(
            self.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        return "sha256:" + hashlib.sha256(raw).hexdigest()


@dataclass(frozen=True)
class ControlDecision:
    action: ControlAction
    reason: str = ""


@dataclass(frozen=True)
class AcceptanceResult:
    check_id: str
    passed: bool
    required: bool
    evidence_refs: tuple[str, ...] = ()


@dataclass(frozen=True)
class ControlledRunResult:
    outcome: RunOutcome
    reason: str
    acceptance: tuple[AcceptanceResult, ...]
    evidence_refs: tuple[str, ...]
    usage: dict[str, int]
    next_action: str
    resumable: bool
    control_spec_digest: str
    completed_milestones: tuple[str, ...] = ()
    experiments: tuple[dict[str, Any], ...] = ()

    def to_payload(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome.value,
            "outcome_reason": self.reason,
            "acceptance": [
                {
                    "check_id": item.check_id,
                    "passed": item.passed,
                    "required": item.required,
                    "evidence_refs": list(item.evidence_refs),
                }
                for item in self.acceptance
            ],
            "evidence_refs": list(self.evidence_refs),
            "usage": dict(self.usage),
            "next_action": self.next_action,
            "resumable": self.resumable,
            "control_spec_digest": self.control_spec_digest,
            "completed_milestones": list(self.completed_milestones),
            "experiments": [dict(item) for item in self.experiments],
        }


@dataclass
class RunController:
    """Run-scoped deterministic controller with checkpointable state."""

    spec: RunControlSpec
    started_at: float = field(default_factory=time.monotonic)
    model_calls: int = 0
    tool_calls: int = 0
    total_tokens: int = 0
    replans: int = 0
    closing_requested: bool = False
    closing_prompt_emitted: bool = False
    stop_reason: str = ""
    budget_exhausted: bool = False
    action_fingerprints: deque[str] = field(default_factory=deque)
    error_fingerprints: deque[str] = field(default_factory=deque)
    acceptance_evidence: dict[str, list[str]] = field(default_factory=dict)
    completed_milestones: set[str] = field(default_factory=set)
    guidance_revision: int = 0
    guidance_emitted_revision: int = -1
    replan_reason: str = ""
    experiments: list[dict[str, Any]] = field(default_factory=list)

    def before_model(self) -> ControlDecision:
        decision = self._budget_decision(next_model_calls=1)
        if decision.action == ControlAction.STOP:
            return decision
        self.model_calls += 1
        return decision

    def before_tool(
        self,
        name: str,
        arguments: Mapping[str, Any],
        *,
        stagnation_exempt: bool = False,
    ) -> ControlDecision:
        decision = self._budget_decision(next_tool_calls=1)
        if decision.action == ControlAction.STOP:
            return decision
        if not stagnation_exempt:
            fingerprint = _fingerprint({"tool": name, "arguments": arguments})
            self._append(self.action_fingerprints, fingerprint)
            if Counter(self.action_fingerprints)[fingerprint] >= (
                self.spec.stagnation.action_repeat_threshold
            ):
                return self._request_replan("repeated_action")
        self.tool_calls += 1
        return decision

    def observe(self, event: RuntimeEvent) -> ControlDecision:
        for check in self.spec.acceptance:
            if _event_matches(check, event):
                refs = self.acceptance_evidence.setdefault(check.check_id, [])
                if event.event_id not in refs:
                    refs.append(event.event_id)
        self._refresh_milestones()
        if event.event_type == EventType.USAGE_REPORTED:
            reported = event.payload.get("total_tokens")
            if reported is None:
                reported = int(event.payload.get("input_tokens") or 0) + int(
                    event.payload.get("output_tokens") or 0
                )
            self.total_tokens += int(reported or 0)
            return self._budget_decision()
        if event.event_type in {EventType.TOOL_CALL_END, EventType.MODEL_CALL_FAILED}:
            error = str(
                event.payload.get("error_category")
                or event.payload.get("failure_category")
                or event.payload.get("error")
                or ""
            )
            if error:
                fingerprint = _fingerprint({"error": error[:512]})
                self._append(self.error_fingerprints, fingerprint)
                if Counter(self.error_fingerprints)[fingerprint] >= (
                    self.spec.stagnation.error_repeat_threshold
                ):
                    return self._request_replan("repeated_error")
        return self._budget_decision()

    def finalize(
        self,
        events: Sequence[RuntimeEvent],
        *,
        terminal_error: str | None = None,
    ) -> ControlledRunResult:
        acceptance = tuple(
            _evaluate_check(
                check,
                events,
                preserved_refs=self.acceptance_evidence.get(check.check_id, ()),
            )
            for check in self.spec.acceptance
        )
        required = [item for item in acceptance if item.required]
        passed_required = [item for item in required if item.passed]
        any_passed = any(item.passed for item in acceptance)
        if terminal_error:
            outcome = RunOutcome.FAILED
            reason = terminal_error
        elif self.budget_exhausted:
            outcome = RunOutcome.BUDGET_EXHAUSTED
            reason = self.stop_reason or "hard_limit_reached"
        elif self.stop_reason:
            outcome = RunOutcome.PARTIAL if any_passed else RunOutcome.BLOCKED
            reason = self.stop_reason
        elif required and len(passed_required) == len(required):
            outcome = RunOutcome.VERIFIED
            reason = "all_required_acceptance_passed"
        elif any_passed:
            outcome = RunOutcome.PARTIAL
            reason = "required_acceptance_incomplete"
        else:
            outcome = RunOutcome.UNVERIFIED
            reason = "no_required_acceptance_verified"
        evidence = tuple(
            dict.fromkeys(
                ref for item in acceptance for ref in item.evidence_refs
            )
        )
        # 当前 Revision 的控制合同不可变；硬预算耗尽后不能靠 resume 绕过，
        # 必须创建带新预算的 Revision/Run。阻塞与部分结果保留恢复语义。
        resumable = outcome in {RunOutcome.PARTIAL, RunOutcome.BLOCKED}
        return ControlledRunResult(
            outcome=outcome,
            reason=reason,
            acceptance=acceptance,
            evidence_refs=evidence,
            usage={
                "model_calls": self.model_calls,
                "tool_calls": self.tool_calls,
                "total_tokens": self.total_tokens,
            },
            next_action=(
                "resolve the reported blocker and resume"
                if resumable
                else (
                    "start a new revision/run with an explicitly increased budget"
                    if outcome == RunOutcome.BUDGET_EXHAUSTED
                    else "none"
                )
            ),
            resumable=resumable,
            control_spec_digest=self.spec.digest,
            completed_milestones=tuple(
                item.milestone_id
                for item in self.spec.milestones
                if item.milestone_id in self.completed_milestones
            ),
            experiments=tuple(dict(item) for item in self.experiments),
        )

    def snapshot(self) -> dict[str, Any]:
        return {
            "spec_digest": self.spec.digest,
            "elapsed_seconds": max(0.0, time.monotonic() - self.started_at),
            "model_calls": self.model_calls,
            "tool_calls": self.tool_calls,
            "total_tokens": self.total_tokens,
            "replans": self.replans,
            "closing_requested": self.closing_requested,
            "closing_prompt_emitted": self.closing_prompt_emitted,
            "stop_reason": self.stop_reason,
            "budget_exhausted": self.budget_exhausted,
            "action_fingerprints": list(self.action_fingerprints),
            "error_fingerprints": list(self.error_fingerprints),
            "acceptance_evidence": {
                key: list(value) for key, value in self.acceptance_evidence.items()
            },
            "completed_milestones": sorted(self.completed_milestones),
            "guidance_revision": self.guidance_revision,
            "guidance_emitted_revision": self.guidance_emitted_revision,
            "replan_reason": self.replan_reason,
            "experiments": [dict(item) for item in self.experiments],
        }

    def restore(self, snapshot: Mapping[str, Any]) -> None:
        digest = str(snapshot.get("spec_digest") or "")
        if digest and digest != self.spec.digest:
            raise ValueError("run control snapshot/spec digest mismatch")
        elapsed = max(0.0, float(snapshot.get("elapsed_seconds") or 0.0))
        self.started_at = time.monotonic() - elapsed
        self.model_calls = int(snapshot.get("model_calls") or 0)
        self.tool_calls = int(snapshot.get("tool_calls") or 0)
        self.total_tokens = int(snapshot.get("total_tokens") or 0)
        self.replans = int(snapshot.get("replans") or 0)
        self.closing_requested = bool(snapshot.get("closing_requested"))
        self.closing_prompt_emitted = bool(snapshot.get("closing_prompt_emitted"))
        self.stop_reason = str(snapshot.get("stop_reason") or "")
        self.budget_exhausted = bool(snapshot.get("budget_exhausted"))
        self.action_fingerprints = deque(
            (str(item) for item in snapshot.get("action_fingerprints") or ()),
            maxlen=self.spec.stagnation.fingerprint_window,
        )
        self.error_fingerprints = deque(
            (str(item) for item in snapshot.get("error_fingerprints") or ()),
            maxlen=self.spec.stagnation.fingerprint_window,
        )
        self.acceptance_evidence = {
            str(key): [str(item) for item in value]
            for key, value in (snapshot.get("acceptance_evidence") or {}).items()
            if isinstance(value, (list, tuple))
        }
        self.completed_milestones = {
            str(item) for item in snapshot.get("completed_milestones") or ()
        }
        self.guidance_revision = int(snapshot.get("guidance_revision") or 0)
        self.guidance_emitted_revision = int(
            snapshot.get("guidance_emitted_revision")
            if snapshot.get("guidance_emitted_revision") is not None
            else -1
        )
        self.replan_reason = str(snapshot.get("replan_reason") or "")
        self.experiments = [
            dict(item)
            for item in snapshot.get("experiments") or ()
            if isinstance(item, Mapping)
        ]

    def closing_instruction(self, reason: str) -> str:
        return (
            "运行控制器要求立即收口。不得启动新的探索性动作。请基于已经获得的"
            "证据说明：已完成项、未完成项、限制、可恢复的下一步。"
            f"控制原因：{reason}。目标：{self.spec.objective}"
        )

    def take_closing_instruction(self, reason: str) -> str | None:
        """Return the closing instruction once, including after checkpoint restore."""

        if self.closing_prompt_emitted:
            return None
        self.closing_prompt_emitted = True
        return self.closing_instruction(reason)

    def take_execution_instruction(self) -> str | None:
        """Return current objective/milestone guidance once per plan revision."""

        if self.guidance_emitted_revision == self.guidance_revision:
            return None
        self.guidance_emitted_revision = self.guidance_revision
        active = self.next_milestone()
        milestone_text = (
            "当前里程碑："
            f"{active.milestone_id} — {active.description}；"
            f"验收项：{', '.join(active.acceptance_ids)}。"
            if active is not None
            else "当前没有未完成且依赖已满足的里程碑。"
        )
        replan = f"重规划原因：{self.replan_reason}。" if self.replan_reason else ""
        return (
            "这是运行控制器提供的低频执行合同。最终完成状态只由外部验收证据决定，"
            "不要自报完成。"
            f"总目标：{self.spec.objective}。{milestone_text}{replan}"
        )

    def next_milestone(self) -> MilestoneSpec | None:
        for milestone in self.spec.milestones:
            if milestone.milestone_id in self.completed_milestones:
                continue
            if set(milestone.depends_on) <= self.completed_milestones:
                return milestone
        return None

    def record_experiment(
        self,
        experiment_id: str,
        *,
        baseline_score: float,
        candidate_score: float,
        minimum_improvement: float = 0,
        evidence_refs: Sequence[str] = (),
    ) -> ExperimentDecision:
        """Deterministically keep or roll back a candidate using external scores."""

        if not experiment_id.strip():
            raise ValueError("experiment_id is required")
        if any(item.get("experiment_id") == experiment_id for item in self.experiments):
            raise ValueError(f"duplicate experiment_id: {experiment_id}")
        if not all(
            math.isfinite(value)
            for value in (baseline_score, candidate_score, minimum_improvement)
        ):
            raise ValueError("experiment scores must be finite")
        if minimum_improvement < 0:
            raise ValueError("minimum_improvement cannot be negative")
        decision = (
            ExperimentDecision.KEEP
            if candidate_score >= baseline_score + minimum_improvement
            else ExperimentDecision.ROLLBACK
        )
        self.experiments.append(
            {
                "experiment_id": experiment_id,
                "baseline_score": float(baseline_score),
                "candidate_score": float(candidate_score),
                "minimum_improvement": float(minimum_improvement),
                "decision": decision.value,
                "evidence_refs": list(dict.fromkeys(str(ref) for ref in evidence_refs)),
            }
        )
        return decision

    def _budget_decision(
        self,
        *,
        next_model_calls: int = 0,
        next_tool_calls: int = 0,
    ) -> ControlDecision:
        if self.stop_reason:
            return ControlDecision(ControlAction.STOP, self.stop_reason)
        limits = self.spec.limits
        elapsed = time.monotonic() - self.started_at
        checks = (
            ("wall_time", elapsed, limits.max_wall_seconds, 0.0),
            ("model_calls", self.model_calls, limits.max_model_calls, next_model_calls),
            ("tool_calls", self.tool_calls, limits.max_tool_calls, next_tool_calls),
            ("total_tokens", self.total_tokens, limits.max_total_tokens, 0),
        )
        for name, used, limit, pending in checks:
            if limit is not None and used + pending > limit:
                self.stop_reason = f"{name}_hard_limit"
                self.budget_exhausted = True
                return ControlDecision(ControlAction.STOP, self.stop_reason)
        for name, used, limit, pending in checks:
            if limit is not None and used + pending >= limit * limits.soft_limit_ratio:
                self.closing_requested = True
                return ControlDecision(ControlAction.CLOSE, f"{name}_soft_limit")
        return ControlDecision(ControlAction.CONTINUE)

    def _request_replan(self, reason: str) -> ControlDecision:
        if self.replans >= self.spec.stagnation.max_replans:
            self.stop_reason = f"stagnation_after_{self.replans}_replans:{reason}"
            return ControlDecision(ControlAction.STOP, self.stop_reason)
        self.replans += 1
        self.guidance_revision += 1
        self.replan_reason = reason
        return ControlDecision(ControlAction.REPLAN, reason)

    def _append(self, target: deque[str], value: str) -> None:
        target.append(value)
        while len(target) > self.spec.stagnation.fingerprint_window:
            target.popleft()

    def _refresh_milestones(self) -> None:
        for milestone in self.spec.milestones:
            if milestone.milestone_id in self.completed_milestones:
                continue
            if not set(milestone.depends_on) <= self.completed_milestones:
                continue
            if all(self.acceptance_evidence.get(item) for item in milestone.acceptance_ids):
                self.completed_milestones.add(milestone.milestone_id)
                self.guidance_revision += 1


def _evaluate_check(
    check: AcceptanceCheck,
    events: Sequence[RuntimeEvent],
    *,
    preserved_refs: Sequence[str] = (),
) -> AcceptanceResult:
    evidence = list(preserved_refs)
    evidence.extend(event.event_id for event in events if _event_matches(check, event))
    evidence = list(dict.fromkeys(evidence))
    return AcceptanceResult(
        check_id=check.check_id,
        passed=bool(evidence),
        required=check.required,
        evidence_refs=tuple(evidence),
    )


def _event_matches(check: AcceptanceCheck, event: RuntimeEvent) -> bool:
    if check.kind == "text_contains":
        return event.event_type == EventType.TEXT_COMPLETED and check.expected in str(
            event.payload.get("text") or ""
        )
    if check.kind == "event_exists":
        return event.event_type == check.expected
    if check.kind == "tool_succeeded":
        return (
            event.event_type == EventType.TOOL_CALL_END
            and event.payload.get("name") == check.expected
            and not event.payload.get("error")
        )
    if check.kind == "artifact_exists":
        return (
            event.event_type == EventType.ARTIFACT_CREATED
            and event.payload.get("name") == check.expected
        )
    return False


def _fingerprint(value: Any) -> str:
    raw = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode()
    return hashlib.sha256(raw).hexdigest()


def _assert_acyclic_milestones(milestones: Sequence[MilestoneSpec]) -> None:
    dependencies = {item.milestone_id: set(item.depends_on) for item in milestones}
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node: str) -> None:
        if node in visiting:
            raise ValueError("milestone dependencies must be acyclic")
        if node in visited:
            return
        visiting.add(node)
        for dependency in dependencies[node]:
            visit(dependency)
        visiting.remove(node)
        visited.add(node)

    for milestone_id in dependencies:
        visit(milestone_id)


def run_control_spec_from_config(config: Mapping[str, Any]) -> RunControlSpec | None:
    """Parse the immutable control contract from execution strategy config."""

    raw = config.get("run_control")
    if raw is None:
        return None
    if isinstance(raw, RunControlSpec):
        return raw
    if not isinstance(raw, Mapping):
        raise ValueError("execution_strategy.config.run_control must be an object")
    return RunControlSpec.model_validate(raw)


__all__ = [
    "AcceptanceCheck",
    "AcceptanceResult",
    "ControlAction",
    "ControlDecision",
    "ControlledRunResult",
    "ExperimentDecision",
    "MilestoneSpec",
    "RunControlSpec",
    "RunControlReplan",
    "RunControlStop",
    "RunController",
    "RunLimits",
    "RunOutcome",
    "StagnationPolicy",
    "run_control_spec_from_config",
]

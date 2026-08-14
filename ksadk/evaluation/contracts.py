"""Stable, serializable data objects shared by CLI and Studio evaluation."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

# ---------------------------------------------------------------------------
# Base model & helpers
# ---------------------------------------------------------------------------


def _to_camel_case(value: str) -> str:
    head, *tail = value.split("_")
    return head + "".join(part.capitalize() for part in tail)


def _content_digest(payload: dict[str, Any]) -> str:
    import hashlib
    import json

    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class EvaluationModel(BaseModel):
    """Use camelCase on the wire while keeping Python fields snake_case."""

    model_config = ConfigDict(
        alias_generator=_to_camel_case,
        populate_by_name=True,
        extra="forbid",
    )


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


# Assertion kinds & data egress policy (configure how cases are asserted and
# what data may leave the local process).
class AssertionType(str, Enum):
    """Deterministic assertion kinds supported by the first evaluation phase."""

    RESPONSE_EQUALS = "response.equals"
    RESPONSE_CONTAINS = "response.contains"
    RESPONSE_NOT_CONTAINS = "response.notContains"
    RESPONSE_JSON_SCHEMA = "response.jsonSchema"
    RUNTIME_MAX_LATENCY_MS = "runtime.maxLatencyMs"
    RUNTIME_MAX_INPUT_TOKENS = "runtime.maxInputTokens"
    RUNTIME_MAX_OUTPUT_TOKENS = "runtime.maxOutputTokens"
    TOOL_CALLED = "tool.called"
    TOOL_NOT_CALLED = "tool.notCalled"


class DataPolicy(str, Enum):
    """Controls what evaluation data may leave the local process."""

    LOCAL_ONLY = "local_only"
    METADATA_ONLY = "metadata_only"
    REDACTED_TRACE = "redacted_trace"
    FULL_TRACE = "full_trace"


# Lifecycle & outcome statuses (run-level, per-case target invocation, and
# per-metric verdicts).
class EvalRunStatus(str, Enum):
    """Lifecycle status for an entire evaluation run."""

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    PASSED = "PASSED"
    FAILED = "FAILED"
    ERROR = "ERROR"
    CANCELLED = "CANCELLED"


class TargetRunStatus(str, Enum):
    """Terminal status returned by one target invocation."""

    PASSED = "PASSED"
    FAILED = "FAILED"
    ERROR = "ERROR"
    CANCELLED = "CANCELLED"
    UNAVAILABLE = "UNAVAILABLE"


class MetricStatus(str, Enum):
    """Outcome of a single deterministic metric."""

    PASS = "PASS"
    FAIL = "FAIL"
    UNAVAILABLE = "UNAVAILABLE"
    ERROR = "ERROR"


# ---------------------------------------------------------------------------
# Evaluation set & input cases
# ---------------------------------------------------------------------------


class EvalTurn(EvaluationModel):
    """A single conversational turn within a multi-turn case."""

    input: str = Field(min_length=1, max_length=32768)
    expected_output: str | None = Field(default=None, max_length=32768)
    expected_tools: list[dict[str, Any]] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class AssertionSpec(EvaluationModel):
    """A single deterministic assertion with type-dependent value validation."""

    type: AssertionType
    value: Any
    required: bool = True

    @model_validator(mode="after")
    def validate_value(self) -> "AssertionSpec":
        if self.type is AssertionType.RESPONSE_JSON_SCHEMA:
            if not isinstance(self.value, dict):
                raise ValueError("response.jsonSchema 的 value 必须是对象")
        elif self.type.value.startswith("runtime."):
            if isinstance(self.value, bool) or not isinstance(self.value, (int, float)):
                raise ValueError(f"{self.type} 的 value 必须是非负数字")
            if self.value < 0:
                raise ValueError(f"{self.type} 的 value 必须是非负数字")
        elif not isinstance(self.value, str):
            raise ValueError(f"{self.type} 的 value 必须是字符串")
        return self


class EvalCase(EvaluationModel):
    """One evaluation case: ordered turns plus assertions to check."""

    id: str = Field(min_length=1, max_length=128)
    turns: list[EvalTurn] = Field(min_length=1)
    assertions: list[AssertionSpec] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def normalize_single_input(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        data = dict(value)
        if "input" in data and "turns" not in data:
            turn = {"input": data.pop("input")}
            for field in ("expected_output", "expectedOutput", "expected_tools", "expectedTools"):
                if field in data:
                    turn[field] = data.pop(field)
            data["turns"] = [turn]
        return data

    @property
    def input(self) -> str:
        """Return the final turn for legacy single-input consumers."""

        return self.turns[-1].input


class EvalSetVersion(EvaluationModel):
    """A versioned, content-digested evaluation set of one or more cases."""

    schema_version: Literal["ksadk.eval/v1"] = "ksadk.eval/v1"
    name: str = Field(min_length=1, max_length=256)
    cases: list[EvalCase] = Field(min_length=1)
    metadata: dict[str, Any] = Field(default_factory=dict)
    source_format: str = "native"
    content_digest: str = ""

    @model_validator(mode="after")
    def validate_and_digest(self) -> "EvalSetVersion":
        ids = [case.id for case in self.cases]
        if len(ids) != len(set(ids)):
            raise ValueError("评测集中的 case id 必须唯一")
        expected = self.compute_digest()
        if self.content_digest and self.content_digest != expected:
            raise ValueError("contentDigest 与规范化评测集内容不一致")
        self.content_digest = expected
        return self

    def compute_digest(self) -> str:
        """Hash normalized cases, excluding transport format and the digest itself."""

        payload = self.model_dump(
            mode="json",
            by_alias=False,
            exclude={"content_digest", "source_format"},
        )
        return _content_digest(payload)


# ---------------------------------------------------------------------------
# Target identity & references
# ---------------------------------------------------------------------------


class TargetKind(str, Enum):
    """Supported target categories; adapters own their implementation details."""

    LOCAL_SOURCE = "local_source"
    STUDIO_BUILD = "studio_build"
    A2A = "a2a"
    CODEX_WORKTREE = "codex_worktree"


class TargetSnapshot(EvaluationModel):
    """Immutable target identity recorded with an evaluation run."""

    kind: TargetKind
    entrypoint: str = Field(min_length=1, max_length=1024)
    revision_digest: str = Field(min_length=1, max_length=128)
    runtime: str = Field(default="", max_length=128)
    metadata: dict[str, Any] = Field(default_factory=dict)


class TargetRef(EvaluationModel):
    """Unresolved target supplied by CLI/Studio before an adapter snapshots it."""

    kind: TargetKind
    locator: str = Field(min_length=1, max_length=2048)
    entrypoint: str | None = Field(default=None, min_length=1, max_length=1024)
    credential_ref: str | None = Field(default=None, min_length=1, max_length=512)
    profile: str | None = Field(default=None, min_length=1, max_length=256)


# ---------------------------------------------------------------------------
# Execution config & request / spec
# ---------------------------------------------------------------------------


class EvaluationConfig(EvaluationModel):
    """Execution limits and evaluator selection shared by CLI and Studio."""

    timeout_seconds: int = Field(default=120, ge=1, le=3600)
    fail_fast: bool = False
    evaluators: list[str] = Field(default_factory=list)
    data_policy: DataPolicy = DataPolicy.LOCAL_ONLY
    judge_model: str | None = Field(default=None, min_length=1, max_length=256)
    judge_api_base: str | None = Field(default=None, min_length=1, max_length=2048)
    judge_api_key_env: str = Field(
        default="KSADK_EVAL_JUDGE_API_KEY",
        pattern=r"^[A-Za-z_][A-Za-z0-9_]*$",
        max_length=128,
    )


class EvaluationRequest(EvaluationModel):
    """Validated input passed into the one public evaluation executor."""

    evalset: EvalSetVersion
    target: TargetRef
    config: EvaluationConfig = Field(default_factory=EvaluationConfig)
    report_dir: str | None = Field(default=None, min_length=1, max_length=2048)


class EvalRunSpec(EvaluationModel):
    """Immutable execution plan after the target has been snapshotted."""

    id: str = Field(min_length=1, max_length=128)
    evalset: EvalSetVersion
    target: TargetSnapshot
    config: EvaluationConfig = Field(default_factory=EvaluationConfig)
    environment_digest: str = Field(default="", max_length=128)
    attempt: int = Field(default=1, ge=1)


# ---------------------------------------------------------------------------
# Run results: trace, usage, target run, metric results, case run, report
# ---------------------------------------------------------------------------


# Per-case adapter output: trace linkage, token usage, and the normalized
# invocation result returned by a target adapter.
class TraceRef(EvaluationModel):
    """Queryable linkage to a recorded trace; at least one id required."""

    run_id: str | None = None
    trace_id: str | None = None
    root_span_id: str | None = None
    seq_id: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def require_reference(self) -> "TraceRef":
        if not any((self.run_id, self.trace_id, self.seq_id)):
            raise ValueError("TraceRef 至少需要一个可查询 ID")
        return self


class UsageSnapshot(EvaluationModel):
    """Token usage for one invocation; zero unless the adapter reported it."""

    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)
    reported: bool = False


class TargetRun(EvaluationModel):
    """Normalized result returned by a target adapter for one case."""

    status: TargetRunStatus
    output: str = ""
    duration_ms: int | None = Field(default=None, ge=0)
    usage: UsageSnapshot = Field(default_factory=UsageSnapshot)
    error_code: str | None = None
    error_message: str | None = None
    trace_ref: TraceRef | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


# Evaluator output: per-metric verdicts, per-case aggregation, run-level
# summary counts, and the canonical persisted report.
class MetricResult(EvaluationModel):
    """One deterministic evaluator result and its non-sensitive evidence."""

    name: str = Field(min_length=1, max_length=128)
    version: str = Field(default="v1", min_length=1, max_length=32)
    status: MetricStatus
    score: float | None = Field(default=None, ge=0, le=1)
    required: bool = True
    evidence: dict[str, Any] = Field(default_factory=dict)


class CaseRun(EvaluationModel):
    """Target result and metric results for one case attempt."""

    case_id: str = Field(min_length=1, max_length=128)
    attempt: int = Field(default=1, ge=1)
    target_run: TargetRun
    metrics: list[MetricResult] = Field(default_factory=list)

    @property
    def passed(self) -> bool:
        return self.target_run.status is TargetRunStatus.PASSED and all(
            metric.status is MetricStatus.PASS for metric in self.metrics if metric.required
        )


class EvalRunSummary(EvaluationModel):
    """Derived terminal counts; callers must not construct their own summary."""

    total_cases: int = Field(default=0, ge=0)
    passed_cases: int = Field(default=0, ge=0)
    failed_cases: int = Field(default=0, ge=0)
    unavailable_cases: int = Field(default=0, ge=0)
    error_cases: int = Field(default=0, ge=0)
    cancelled_cases: int = Field(default=0, ge=0)


class EvalRunReport(EvaluationModel):
    """Canonical persisted result consumed by CLI, Studio, and future sync."""

    schema_version: Literal["ksadk.eval.report/v1"] = "ksadk.eval.report/v1"
    spec: EvalRunSpec
    status: EvalRunStatus
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    case_runs: list[CaseRun] = Field(default_factory=list)
    summary: EvalRunSummary = Field(default_factory=EvalRunSummary)
    report_digest: str = ""

    @model_validator(mode="after")
    def validate_case_ids(self) -> "EvalRunReport":
        case_ids = [case.case_id for case in self.case_runs]
        if len(case_ids) != len(set(case_ids)):
            raise ValueError("一次评测中 case_id 不能重复")
        self.summary = self._summarize_cases()
        expected = self.compute_digest()
        if self.report_digest and self.report_digest != expected:
            raise ValueError("reportDigest 与规范化报告内容不一致")
        self.report_digest = expected
        return self

    def _summarize_cases(self) -> EvalRunSummary:
        counts = EvalRunSummary(total_cases=len(self.case_runs))
        for case_run in self.case_runs:
            if case_run.target_run.status is TargetRunStatus.ERROR or any(
                metric.required and metric.status is MetricStatus.ERROR
                for metric in case_run.metrics
            ):
                counts.error_cases += 1
            elif case_run.target_run.status is TargetRunStatus.CANCELLED:
                counts.cancelled_cases += 1
            elif case_run.target_run.status is TargetRunStatus.UNAVAILABLE or any(
                metric.required and metric.status is MetricStatus.UNAVAILABLE
                for metric in case_run.metrics
            ):
                counts.unavailable_cases += 1
            elif case_run.passed:
                counts.passed_cases += 1
            else:
                counts.failed_cases += 1
        return counts

    def compute_digest(self) -> str:
        """Return a stable digest excluding the digest field itself."""

        payload = self.model_dump(mode="json", by_alias=False, exclude={"report_digest"})
        return _content_digest(payload)

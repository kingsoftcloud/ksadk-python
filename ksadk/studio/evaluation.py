"""Local evaluation suites and deterministic assertions."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import cast
from uuid import uuid4

from jsonschema import (  # type: ignore[import-untyped]
    ValidationError as JSONSchemaValidationError,
)
from jsonschema import validate as validate_json  # type: ignore[import-untyped]

from ksadk.studio.contracts import (
    AssertionResult,
    AssertionSpec,
    EvaluationCaseResult,
    EvaluationRun,
    EvaluationSuite,
    RunRecord,
    RunStatus,
)
from ksadk.studio.errors import StudioError
from ksadk.studio.event_store import RunEventStore
from ksadk.studio.repository import BuildRepository, load_yaml_file
from ksadk.studio.workspace import Workspace


class EvaluationRunner:
    def __init__(
        self,
        workspace: Workspace,
        *,
        run_agent: Callable[[str, str, str | None], Awaitable[RunRecord]],
        event_store: RunEventStore,
        build_repository: BuildRepository | None = None,
    ) -> None:
        self.workspace = workspace
        self.run_agent = run_agent
        self.event_store = event_store
        self.build_repository = build_repository or BuildRepository(workspace)

    async def run(
        self,
        build_id: str,
        suite_refs: list[str],
        *,
        fail_fast: bool = False,
    ) -> EvaluationRun:
        build = self.build_repository.get(build_id)
        suites = [
            self._load_suite(build.agent_id, reference) for reference in suite_refs
        ]
        evaluation = EvaluationRun(
            id=f"eval_{uuid4().hex}",
            build_id=build_id,
            status=RunStatus.RUNNING,
        )
        for suite in suites:
            for case in suite.cases:
                run = await self.run_agent(
                    build_id,
                    case.input,
                    f"eval_{evaluation.id}_{case.id}",
                )
                assertion_results = [
                    self._assert(assertion, run) for assertion in case.assertions
                ]
                passed = run.status == RunStatus.COMPLETED and all(
                    result.passed for result in assertion_results
                )
                evaluation.results.append(
                    EvaluationCaseResult(
                        case_id=case.id,
                        run_id=run.id,
                        passed=passed,
                        assertions=assertion_results,
                    )
                )
                if fail_fast and not passed:
                    break
            if fail_fast and evaluation.results and not evaluation.results[-1].passed:
                break
        evaluation.total = len(evaluation.results)
        evaluation.passed = sum(1 for result in evaluation.results if result.passed)
        evaluation.failed = evaluation.total - evaluation.passed
        evaluation.pass_rate = (
            evaluation.passed / evaluation.total if evaluation.total else 0
        )
        evaluation.status = (
            RunStatus.COMPLETED if evaluation.failed == 0 else RunStatus.FAILED
        )
        self._save(evaluation)
        return evaluation

    def get(self, evaluation_id: str) -> EvaluationRun:
        path = self.workspace.resolve(
            Path(".agentkit/evaluations") / f"{evaluation_id}.json"
        )
        if not path.is_file():
            raise StudioError(
                "EVALUATION_NOT_FOUND",
                "Evaluation 不存在",
                status_code=404,
                details={"id": evaluation_id},
            )
        return cast(
            EvaluationRun,
            EvaluationRun.model_validate_json(path.read_text(encoding="utf-8")),
        )

    def _load_suite(self, agent_id: str, reference: str) -> EvaluationSuite:
        candidates = [
            Path("agents") / agent_id / reference,
            Path(reference),
        ]
        for candidate in candidates:
            path = self.workspace.resolve(candidate)
            if path.is_file():
                try:
                    return cast(
                        EvaluationSuite,
                        EvaluationSuite.model_validate(load_yaml_file(path)),
                    )
                except ValueError as exc:
                    raise StudioError(
                        "EVALUATION_SUITE_INVALID",
                        "评测集格式无效",
                        status_code=422,
                        details={"reference": reference, "reason": str(exc)},
                    ) from exc
        raise StudioError(
            "EVALUATION_SUITE_NOT_FOUND",
            "评测集不存在",
            status_code=404,
            details={"reference": reference},
        )

    def _assert(self, assertion: AssertionSpec, run: RunRecord) -> AssertionResult:
        value = assertion.value
        passed = False
        message = ""
        if assertion.type == "contains":
            passed = str(value) in run.output
        elif assertion.type == "equals":
            passed = run.output == str(value)
        elif assertion.type == "notContains":
            passed = str(value) not in run.output
        elif assertion.type == "maxLatencyMs":
            passed = (run.duration_ms or 0) <= int(value)
        elif assertion.type == "maxInputTokens":
            passed = run.usage.input_tokens <= int(value)
        elif assertion.type == "maxOutputTokens":
            passed = run.usage.output_tokens <= int(value)
        elif assertion.type == "jsonSchema":
            try:
                validate_json(json.loads(run.output), value)
                passed = True
            except (ValueError, JSONSchemaValidationError) as exc:
                message = str(exc)
        elif assertion.type in {"toolCalled", "toolNotCalled"}:
            called = {
                event.data.get("tool")
                for event in self.event_store.events(run.id)
                if event.type == "tool.requested"
            }
            passed = (
                str(value) in called
                if assertion.type == "toolCalled"
                else str(value) not in called
            )
        if not message and not passed:
            message = f"断言 {assertion.type} 未通过"
        return AssertionResult(assertion=assertion, passed=passed, message=message)

    def _save(self, evaluation: EvaluationRun) -> None:
        directory = self.workspace.resolve(".agentkit/evaluations")
        directory.mkdir(parents=True, exist_ok=True)
        self.workspace.atomic_write_text(
            directory / f"{evaluation.id}.json",
            json.dumps(
                evaluation.model_dump(
                    by_alias=True, exclude_none=True, mode="json"
                ),
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
            )
            + "\n",
        )

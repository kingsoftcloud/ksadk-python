"""agentengine eval - stable CLI shell for local evaluation."""

from __future__ import annotations

import asyncio
from pathlib import Path

import click

from ksadk.cli.ui import (
    configure_ui_runtime,
    emit_json,
    is_json_output,
    print_kv,
    print_success,
    print_title,
)
from ksadk.evaluation import (
    EvaluationConfig,
    EvaluationExecutionError,
    EvaluationNotImplementedError,
    EvaluationRequest,
    TargetRef,
    execute_evaluation,
    load_evalset,
)
from ksadk.evaluation.contracts import (
    DataPolicy,
    EvalRunReport,
    EvalRunStatus,
    MetricStatus,
    TargetKind,
)
from ksadk.evaluation.evalset import EvalSetParseError

_EVALUATORS = (
    "response_contract@v1",
    "runtime_budget@v1",
    "tool_trajectory@v1",
)
_DATA_POLICIES = tuple(policy.value for policy in DataPolicy)


class EvaluationCliError(click.ClickException):
    exit_code = 2


@click.command(context_settings=dict(help_option_names=["-h", "--help"]))
@click.option(
    "--evalset-file",
    required=True,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="本地 EvalSet YAML/JSON 文件",
)
@click.option(
    "--agent-dir",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    help="本地 Agent 项目目录",
)
@click.option("--a2a-url", type=str, help="远端 A2A Agent Card URL")
@click.option(
    "--codex-worktree",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    help="Codex 评测使用的 Git worktree",
)
@click.option("--entrypoint", type=str, help="本地 Agent 入口；缺省时由 target adapter 检测")
@click.option("--credential-ref", type=str, help="A2A 鉴权引用；不会写入凭据值")
@click.option("--codex-profile", type=str, help="Codex 受信评测 profile 名称")
@click.option(
    "--evaluator",
    "evaluators",
    multiple=True,
    type=click.Choice(_EVALUATORS),
    help="启用的确定性评估器；可重复指定",
)
@click.option(
    "--timeout-seconds",
    default=120,
    show_default=True,
    type=click.IntRange(1, 3600),
    help="单个 Case 超时时间（秒）",
)
@click.option("--fail-fast", is_flag=True, help="首个失败 Case 后停止")
@click.option(
    "--data-policy",
    type=click.Choice(_DATA_POLICIES),
    default="local_only",
    show_default=True,
    help="评测数据策略",
)
@click.option(
    "--report-dir",
    type=click.Path(file_okay=False, path_type=Path),
    help="报告目录；缺省为项目 .agentkit/evaluations",
)
@click.option("--validate-only", is_flag=True, help="只识别并校验 EvalSet 和 CLI 参数")
@click.option(
    "--format",
    "output_format",
    type=click.Choice(["pretty", "json"]),
    help="终端输出格式",
)
def eval(
    evalset_file: Path,
    agent_dir: Path | None,
    a2a_url: str | None,
    codex_worktree: Path | None,
    entrypoint: str | None,
    credential_ref: str | None,
    codex_profile: str | None,
    evaluators: tuple[str, ...],
    timeout_seconds: int,
    fail_fast: bool,
    data_policy: str,
    report_dir: Path | None,
    validate_only: bool,
    output_format: str | None,
) -> None:
    """校验 EvalSet，并通过统一入口发起 Agent 评测。"""

    if output_format:
        configure_ui_runtime(output_mode=output_format)
    target = _target_ref(
        agent_dir,
        a2a_url,
        codex_worktree,
        entrypoint,
        credential_ref,
        codex_profile,
    )
    try:
        evalset = load_evalset(evalset_file)
    except EvalSetParseError as exc:
        raise click.UsageError(f"{exc.code}: {exc}") from exc
    request = EvaluationRequest(
        evalset=evalset,
        target=target,
        config=EvaluationConfig(
            timeout_seconds=timeout_seconds,
            fail_fast=fail_fast,
            evaluators=list(evaluators) or list(_EVALUATORS),
            data_policy=data_policy,
        ),
        report_dir=str(report_dir.resolve()) if report_dir else None,
    )
    if validate_only:
        _render_validation(request)
        return
    try:
        report = asyncio.run(execute_evaluation(request))
    except EvaluationNotImplementedError as exc:
        raise EvaluationCliError(str(exc)) from exc
    except EvaluationExecutionError as exc:
        raise EvaluationCliError(str(exc)) from exc
    exit_code = _report_exit_code(report)
    if is_json_output():
        emit_json(report.model_dump(mode="json", by_alias=True, exclude_none=True))
    else:
        print_title("Agent 评测完成")
        print_kv("运行状态", report.status.value)
        print_kv("Run ID", report.spec.id)
    if exit_code:
        raise click.exceptions.Exit(exit_code)


def _report_exit_code(report: EvalRunReport) -> int:
    if report.status in {
        EvalRunStatus.ERROR,
        EvalRunStatus.CANCELLED,
        EvalRunStatus.PENDING,
        EvalRunStatus.RUNNING,
    }:
        return 2
    if any(
        metric.required and metric.status is MetricStatus.UNAVAILABLE
        for case_run in report.case_runs
        for metric in case_run.metrics
    ):
        return 3
    return 1 if report.status is EvalRunStatus.FAILED else 0


def _target_ref(
    agent_dir: Path | None,
    a2a_url: str | None,
    codex_worktree: Path | None,
    entrypoint: str | None,
    credential_ref: str | None,
    codex_profile: str | None,
) -> TargetRef:
    targets = [agent_dir is not None, bool(a2a_url), codex_worktree is not None]
    if sum(targets) != 1:
        raise click.UsageError(
            "--agent-dir、--a2a-url、--codex-worktree 必须且只能指定一个"
        )
    if credential_ref and not a2a_url:
        raise click.UsageError("--credential-ref 只能与 --a2a-url 一起使用")
    if entrypoint and not agent_dir:
        raise click.UsageError("--entrypoint 只能与 --agent-dir 一起使用")
    if codex_profile and not codex_worktree:
        raise click.UsageError("--codex-profile 只能与 --codex-worktree 一起使用")
    if agent_dir:
        return TargetRef(
            kind=TargetKind.LOCAL_SOURCE,
            locator=str(agent_dir.resolve()),
            entrypoint=entrypoint,
        )
    if a2a_url:
        return TargetRef(
            kind=TargetKind.A2A,
            locator=a2a_url,
            credential_ref=credential_ref,
        )
    assert codex_worktree is not None
    return TargetRef(
        kind=TargetKind.CODEX_WORKTREE,
        locator=str(codex_worktree.resolve()),
        profile=codex_profile,
    )


def _render_validation(request: EvaluationRequest) -> None:
    payload = {
        "valid": True,
        "evalset": {
            "name": request.evalset.name,
            "sourceFormat": request.evalset.source_format,
            "caseCount": len(request.evalset.cases),
            "contentDigest": request.evalset.content_digest,
        },
        "target": request.target.model_dump(mode="json", by_alias=True, exclude_none=True),
        "config": request.config.model_dump(mode="json", by_alias=True),
        "reportDir": request.report_dir,
    }
    if is_json_output():
        emit_json(payload)
        return
    print_title("评测配置校验")
    print_kv("EvalSet", request.evalset.name)
    print_kv("格式", request.evalset.source_format)
    print_kv("Case 数量", str(len(request.evalset.cases)))
    print_kv("内容摘要", request.evalset.content_digest)
    print_kv("Target", f"{request.target.kind.value}: {request.target.locator}")
    print_success("EvalSet 与 CLI 参数有效")


# Eval reports use --report-dir exclusively; do not inject the generic child --output.
eval.disable_global_output_option = True

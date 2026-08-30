"""agentengine eval - stable CLI shell for local evaluation."""

from __future__ import annotations

import asyncio
from pathlib import Path

import click
from rich.markup import escape
from rich.padding import Padding
from rich.table import Table

from ksadk.cli.ui import (
    configure_ui_runtime,
    emit_json,
    get_console,
    is_json_output,
    new_table,
    print_info,
    print_kv,
    print_success,
    print_title,
    status_rich_style,
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
from ksadk.evaluation.agent_eval_client import (
    AgentEvalCloudClientError,
    AgentEvalCloudDatasetClient,
)
from ksadk.evaluation.cloud_service import CloudEvalSetPreviewError, CloudEvalSetService
from ksadk.evaluation.contracts import (
    DataPolicy,
    EvalRunReport,
    EvalRunStatus,
    EvalRunSummary,
    MetricResult,
    MetricStatus,
    TargetKind,
    TargetRunStatus,
)
from ksadk.evaluation.evalset import EvalSetParseError
from ksadk.evaluation.evaluators import SUPPORTED_EVALUATORS, resolve_evaluator_plan
from ksadk.evaluation.storage import EvaluationStorage

_DATA_POLICIES = tuple(policy.value for policy in DataPolicy)


class EvaluationCliError(click.ClickException):
    exit_code = 2


@click.command(context_settings=dict(help_option_names=["-h", "--help"]))
@click.option(
    "--evalset-file",
    required=False,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="本地 EvalSet YAML/JSON 文件",
)
@click.option("--dataset-id", type=str, help="云端 Dataset ID；必须配合固定版本使用")
@click.option("--dataset-version", type=click.IntRange(1), help="云端 Dataset immutable version")
@click.option("--dataset-project-id", type=str, help="云端 Dataset 所属项目 ID")
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
    type=click.Choice(SUPPORTED_EVALUATORS),
    help="启用评估器；可重复指定",
)
@click.option("--judge-model", type=str, help="LLM Judge 模型名")
@click.option("--judge-api-base", type=str, help="LLM Judge OpenAI 兼容 API 地址")
@click.option(
    "--judge-api-key-env",
    default="KSADK_EVAL_JUDGE_API_KEY",
    show_default=True,
    type=str,
    help="保存 Judge API Key 的环境变量名",
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
    dataset_id: str | None,
    dataset_version: int | None,
    dataset_project_id: str | None,
    agent_dir: Path | None,
    a2a_url: str | None,
    codex_worktree: Path | None,
    entrypoint: str | None,
    credential_ref: str | None,
    codex_profile: str | None,
    evaluators: tuple[str, ...],
    judge_model: str | None,
    judge_api_base: str | None,
    judge_api_key_env: str,
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
        agent_dir=agent_dir,
        a2a_url=a2a_url,
        codex_worktree=codex_worktree,
        entrypoint=entrypoint,
        credential_ref=credential_ref,
        codex_profile=codex_profile,
    )
    request = _build_request(
        evalset_file=evalset_file,
        dataset_id=dataset_id,
        dataset_version=dataset_version,
        dataset_project_id=dataset_project_id,
        target=target,
        evaluators=evaluators,
        judge_model=judge_model,
        judge_api_base=judge_api_base,
        judge_api_key_env=judge_api_key_env,
        timeout_seconds=timeout_seconds,
        fail_fast=fail_fast,
        data_policy=data_policy,
        report_dir=report_dir,
    )
    if validate_only:
        _render_validation(request)
        return

    report = _execute_request(request)
    exit_code = _report_exit_code(report)
    _render_report(report, report_dir=request.report_dir)
    if exit_code:
        raise click.exceptions.Exit(exit_code)


def _build_request(
    *,
    evalset_file: Path | None,
    dataset_id: str | None,
    dataset_version: int | None,
    dataset_project_id: str | None,
    target: TargetRef,
    evaluators: tuple[str, ...],
    judge_model: str | None,
    judge_api_base: str | None,
    judge_api_key_env: str,
    timeout_seconds: int,
    fail_fast: bool,
    data_policy: str,
    report_dir: Path | None,
) -> EvaluationRequest:
    cloud_dataset = None
    if dataset_id:
        if evalset_file is not None:
            raise click.UsageError("--evalset-file 与 --dataset-id 不能同时使用")
        if dataset_version is None:
            raise click.UsageError("--dataset-id 必须同时指定 --dataset-version")
        try:
            service = CloudEvalSetService(
                Path.cwd(),
                AgentEvalCloudDatasetClient(),
            )
            pulled = asyncio.run(
                service.pull(
                    dataset_id=dataset_id,
                    version=dataset_version,
                    project_id=dataset_project_id,
                )
            )
        except (AgentEvalCloudClientError, CloudEvalSetPreviewError, ValueError) as exc:
            raise click.UsageError(str(exc)) from exc
        evalset = pulled.evalset
        cloud_dataset = pulled.cloud_dataset
    else:
        if evalset_file is None:
            raise click.UsageError("必须指定 --evalset-file 或 --dataset-id")
        if dataset_version is not None or dataset_project_id:
            raise click.UsageError("云端 Dataset 参数必须与 --dataset-id 一起使用")
        try:
            evalset = load_evalset(evalset_file)
        except EvalSetParseError as exc:
            raise click.UsageError(f"{exc.code}: {exc}") from exc

    return EvaluationRequest(
        evalset=evalset,
        target=target,
        config=EvaluationConfig(
            timeout_seconds=timeout_seconds,
            fail_fast=fail_fast,
            evaluators=list(evaluators),
            data_policy=data_policy,
            judge_model=judge_model,
            judge_api_base=judge_api_base,
            judge_api_key_env=judge_api_key_env,
        ),
        report_dir=str((report_dir or Path.cwd() / ".agentkit/evaluations").resolve()),
        cloud_dataset=cloud_dataset,
    )


def _execute_request(request: EvaluationRequest) -> EvalRunReport:
    on_case_started = None
    if not is_json_output():
        _render_start(request)
        on_case_started = _render_case_started
    try:
        return asyncio.run(execute_evaluation(request, on_case_started=on_case_started))
    except (EvaluationNotImplementedError, EvaluationExecutionError) as exc:
        raise EvaluationCliError(str(exc)) from exc


def _render_report(report: EvalRunReport, *, report_dir: str | None = None) -> None:
    if is_json_output():
        emit_json(report.model_dump(mode="json", by_alias=True, exclude_none=True))
    else:
        print_title("Agent 评测完成")
        get_console().print("[title][i]评测摘要[/i][/]")
        print_kv("运行状态", report.status.value)
        print_kv("Run ID", report.spec.id)
        if report_dir:
            print_kv(
                "报告文件",
                str(EvaluationStorage(report_dir).report_path(report.spec.id)),
                value_style="#58a6ff",
            )
        _render_report_preview(report)



def _render_start(request: EvaluationRequest) -> None:
    print_info(
        "开始评测："
        f"{request.evalset.name}，{len(request.evalset.cases)} 个 Case，"
        f"Target: {request.target.kind.value}"
    )


def _render_case_started(case_id: str, index: int, total_cases: int) -> None:
    print_info(f"[{index}/{total_cases}] 执行 Case: {case_id}")


def _render_report_preview(report: EvalRunReport) -> None:
    _render_summary(report.summary)
    _render_case_table(report)
    remaining_cases = len(report.case_runs) - 5
    if remaining_cases > 0:
        print_info(f"其余 {remaining_cases} 个 Case 已省略")


def _render_summary(summary: EvalRunSummary) -> None:
    items = (
        ("总计", summary.total_cases, "white"),
        ("通过", summary.passed_cases, "ok"),
        ("失败", summary.failed_cases, "err"),
        ("错误", summary.error_cases, "err"),
        ("不可用", summary.unavailable_cases, "warn"),
        ("取消", summary.cancelled_cases, "warn"),
    )
    table = Table.grid(padding=(0, 2))
    for _label, _count, style in items:
        table.add_column(justify="center", style=style, no_wrap=True)
    table.add_row(*(label for label, _count, _style in items), style="muted")
    table.add_row(*(f"[{style if count else 'muted'}]{count}[/]" for _label, count, style in items))
    print_info("  结果统计")
    get_console().print(Padding(table, (0, 0, 0, 4)))


def _render_case_table(report: EvalRunReport) -> None:
    table = new_table("  评测集列表")
    table.add_column("Case", style="#58a6ff", no_wrap=True)
    table.add_column("目标状态", no_wrap=True)
    table.add_column("耗时", justify="right", no_wrap=True)
    table.add_column("指标")
    for case_run in report.case_runs[:5]:
        target_status = case_run.target_run.status.value
        table.add_row(
            escape(case_run.case_id),
            f"[{status_rich_style(target_status)}]{target_status}[/]",
            _format_duration(case_run.target_run.duration_ms),
            _metric_summary(case_run.metrics),
        )
    get_console().print(table)


def _format_duration(duration_ms: int | None) -> str:
    return f"{duration_ms} ms" if duration_ms is not None else "-"


def _metric_summary(metrics: list[MetricResult]) -> str:
    if not metrics:
        return "无指标"
    counts = {status: sum(metric.status is status for metric in metrics) for status in MetricStatus}
    return "，".join(f"{status.value} {count}" for status, count in counts.items() if count)


def _report_exit_code(report: EvalRunReport) -> int:
    if report.status in {
        EvalRunStatus.ERROR,
        EvalRunStatus.CANCELLED,
        EvalRunStatus.PENDING,
        EvalRunStatus.RUNNING,
    }:
        return 2
    if any(
        case_run.target_run.status is TargetRunStatus.UNAVAILABLE
        or any(
            metric.required and metric.status is MetricStatus.UNAVAILABLE
            for metric in case_run.metrics
        )
        for case_run in report.case_runs
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
        raise click.UsageError("--agent-dir、--a2a-url、--codex-worktree 必须且只能指定一个")
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
    try:
        evaluation_plan = resolve_evaluator_plan(
            request.evalset.cases,
            request.config.evaluators,
            request.config,
        )
    except ValueError as exc:
        raise click.UsageError(str(exc)) from exc
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
        "evaluationPlan": evaluation_plan,
        "reportDir": request.report_dir,
    }
    if request.cloud_dataset is not None:
        payload["cloudDataset"] = request.cloud_dataset.model_dump(
            mode="json", by_alias=True, exclude_none=True
        )
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

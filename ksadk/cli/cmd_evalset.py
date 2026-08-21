"""Commands for inspecting and publishing immutable cloud EvalSet snapshots."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import click
import yaml

from ksadk.evaluation.agent_eval_client import (
    AgentEvalCloudClientError,
    AgentEvalCloudDatasetClient,
)
from ksadk.evaluation.cloud_service import CloudEvalSetPreviewError, CloudEvalSetService
from ksadk.evaluation.contracts import DataPolicy
from ksadk.evaluation.evalset import EvalSetParseError, load_evalset, parse_evalset

_DATA_POLICIES = tuple(policy.value for policy in DataPolicy)
_TEMPLATE_NAMES = ("knowledge-qa", "structured-output", "tool-routing", "service-sla")
_TEMPLATES: dict[str, dict] = {
    "knowledge-qa": {
        "schemaVersion": "ksadk.eval/v1",
        "name": "knowledge-qa",
        "cases": [
            {
                "id": "capital",
                "input": "中国的首都是哪里？",
                "reference_output": "北京",
            }
        ],
    },
    "structured-output": {
        "schemaVersion": "ksadk.eval/v1",
        "name": "structured-output",
        "cases": [
            {
                "id": "extract-order",
                "input": "从‘订单 A123，金额 99 元’提取订单信息，并只返回 JSON。",
                "assertions": [
                    {
                        "type": "response.jsonSchema",
                        "value": {
                            "type": "object",
                            "required": ["orderId", "amount"],
                            "properties": {
                                "orderId": {"type": "string"},
                                "amount": {"type": "number"},
                            },
                        },
                    }
                ],
            }
        ],
    },
    "tool-routing": {
        "schemaVersion": "ksadk.eval/v1",
        "name": "tool-routing",
        "cases": [
            {
                "id": "weather-lookup",
                "input": "查询北京明天的天气，并给出出行建议。",
                "reference_output": "根据天气查询结果回答北京明天的天气，并给出出行建议。",
                "expectedTools": [{"name": "weather_lookup"}],
                "assertions": [
                    {"type": "tool.succeeded", "value": "weather_lookup"},
                    {"type": "tool.sequence", "value": ["weather_lookup"]},
                ],
            }
        ],
    },
    "service-sla": {
        "schemaVersion": "ksadk.eval/v1",
        "name": "service-sla",
        "cases": [
            {
                "id": "password-reset",
                "input": "如何重置密码？",
                "reference_output": "可通过登录页的忘记密码入口重置密码。",
                "assertions": [
                    {"type": "runtime.maxLatencyMs", "value": 3000},
                    {"type": "runtime.maxTotalTokens", "value": 300},
                ],
            }
        ],
    },
}


def _render(value: dict, output_format: str) -> None:
    if output_format == "json":
        click.echo(json.dumps(value, ensure_ascii=False, sort_keys=True))
        return
    for key, item in value.items():
        click.echo(f"{key}: {item}")


def _load_snapshot(evalset_file: Path, data_policy: str):
    try:
        evalset = load_evalset(evalset_file)
    except EvalSetParseError as exc:
        raise click.UsageError(f"{exc.code}: {exc}") from exc
    service = CloudEvalSetService(Path.cwd(), client=_PreviewOnlyCloudClient())
    try:
        return evalset, service.preview(evalset, data_policy=DataPolicy(data_policy))
    except CloudEvalSetPreviewError as exc:
        raise click.UsageError(str(exc)) from exc


class _PreviewOnlyCloudClient:
    async def publish_snapshot(self, *args, **kwargs):  # pragma: no cover - preview never publishes
        raise RuntimeError("preview does not publish")


@click.group()
def evalset() -> None:
    """Inspect or publish versioned cloud EvalSet snapshots."""


@evalset.command("init")
@click.option("--template", "template_name", type=click.Choice(_TEMPLATE_NAMES), required=True)
@click.option("--output-file", required=True, type=click.Path(dir_okay=False, path_type=Path))
@click.option("--force", is_flag=True, help="覆盖已有文件")
@click.option("--format", "output_format", type=click.Choice(["pretty", "json"]), default="pretty")
def init(template_name: str, output_file: Path, force: bool, output_format: str) -> None:
    """Create a native EvalSet template without accessing cloud services."""

    template = _TEMPLATES[template_name]
    try:
        parse_evalset(template)
    except EvalSetParseError as exc:  # pragma: no cover - protects static templates
        raise click.ClickException(f"内置模板无效: {exc}") from exc

    output = output_file.expanduser().resolve()
    if output.exists() and not force:
        raise click.UsageError("输出文件已存在；如需覆盖请指定 --force")
    if output.exists() and output.is_dir():
        raise click.UsageError("--output-file 必须是文件路径")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        yaml.safe_dump(template, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    _render({"template": template_name, "outputFile": str(output)}, output_format)


@evalset.command("preview")
@click.option(
    "--evalset-file",
    required=True,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
)
@click.option(
    "--data-policy",
    type=click.Choice(_DATA_POLICIES),
    default="full_trace",
    show_default=True,
)
@click.option("--format", "output_format", type=click.Choice(["pretty", "json"]), default="pretty")
def preview(evalset_file: Path, data_policy: str, output_format: str) -> None:
    """Show the exact fixed-schema payload that a push would publish."""
    _evalset, snapshot = _load_snapshot(evalset_file, data_policy)
    _render(snapshot.model_dump(mode="json", by_alias=True, exclude_none=True), output_format)


@evalset.command("push")
@click.option(
    "--file",
    "--evalset-file",
    "evalset_file",
    required=True,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
)
@click.option("--dataset-id")
@click.option("--account-id", envvar="AGENT_EVAL_ACCOUNT_ID", hidden=True)
@click.option("--idempotency-key", hidden=True)
@click.option(
    "--data-policy",
    type=click.Choice(_DATA_POLICIES),
    default="full_trace",
    hidden=True,
)
@click.option("--format", "output_format", type=click.Choice(["pretty", "json"]), default="pretty")
def push(
    evalset_file: Path,
    dataset_id: str | None,
    account_id: str | None,
    idempotency_key: str | None,
    data_policy: str,
    output_format: str,
) -> None:
    """Publish a full EvalSet snapshot to the EvalSmith-backed agent-eval API."""
    workspace = Path.cwd().resolve()
    try:
        evalset_path = evalset_file.resolve().relative_to(workspace).as_posix()
    except ValueError as exc:
        raise click.UsageError("--file must be inside the current workspace") from exc
    evalset, _snapshot = _load_snapshot(evalset_file, data_policy)
    client = AgentEvalCloudDatasetClient(
        account_id=account_id,
    )
    service = CloudEvalSetService(workspace, client)
    try:
        result = asyncio.run(
            service.publish(
                evalset,
                evalset_path=evalset_path,
                dataset_id=dataset_id,
                data_policy=DataPolicy(data_policy),
                idempotency_key=idempotency_key,
            )
        )
    except (AgentEvalCloudClientError, CloudEvalSetPreviewError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    _render(
        {
            "datasetId": result.dataset_id,
            "datasetVersion": result.dataset_version,
            "projectId": result.project_id,
            "schemaHash": result.schema_hash,
            "contentDigest": result.content_digest,
            "rowCount": result.row_count,
        },
        output_format,
    )


@evalset.command("pull")
@click.option("--dataset-id", required=True)
@click.option("--dataset-version", required=True, type=click.IntRange(1))
@click.option("--project-id")
@click.option("--output-file", required=True, type=click.Path(dir_okay=False, path_type=Path))
@click.option("--format", "output_format", type=click.Choice(["pretty", "json"]), default="pretty")
def pull(
    dataset_id: str,
    dataset_version: int,
    project_id: str | None,
    output_file: Path,
    output_format: str,
) -> None:
    """Read one immutable cloud Dataset version into a local EvalSet file."""
    workspace = Path.cwd().resolve()
    target = output_file.resolve()
    try:
        target.relative_to(workspace)
    except ValueError as exc:
        raise click.UsageError("--output-file must be inside the current workspace") from exc

    service = CloudEvalSetService(
        workspace,
        AgentEvalCloudDatasetClient(),
    )
    try:
        result = asyncio.run(
            service.pull(
                dataset_id=dataset_id,
                version=dataset_version,
                project_id=project_id,
            )
        )
    except (AgentEvalCloudClientError, CloudEvalSetPreviewError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        yaml.safe_dump(
            result.evalset.model_dump(mode="json", by_alias=True, exclude_none=True),
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    _render(
        {
            "outputFile": target.relative_to(workspace).as_posix(),
            "datasetId": result.cloud_dataset.dataset_id,
            "datasetVersion": result.cloud_dataset.version,
            "schemaHash": result.cloud_dataset.schema_hash,
            "contentDigest": result.cloud_dataset.content_digest,
            "rowCount": result.cloud_dataset.row_count,
        },
        output_format,
    )

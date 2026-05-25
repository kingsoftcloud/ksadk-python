"""Shared helpers for build/deploy/launch workflows."""

from __future__ import annotations

from dataclasses import dataclass, replace
import json
from pathlib import Path
from typing import Any, Sequence

import click

from ksadk.cli.dry_run import sanitize_dry_run_request
from ksadk.cli.ui import (
    emit_json,
    is_json_output,
    print_info,
    print_kv,
    print_next_steps,
    print_title,
    print_warn,
    print_rule,
)


@dataclass(frozen=True)
class ArtifactBuildPlan:
    """Workflow-level artifact build decision."""

    should_build: bool
    should_clear_metadata: bool
    explicit_ref_option: str | None = None
    will_build: bool | None = None
    should_publish: bool | None = None
    will_publish: bool | None = None
    source: str | None = None
    reference: str | None = None
    reference_is_predicted: bool = False


def build_workflow_local_plan(
    *,
    project_dir: Path,
    framework: str,
    target: str,
    region: str | None,
    deploy_name: str | None,
    artifact_type: str | None,
    artifact_plan: ArtifactBuildPlan,
    build_dir: str | None,
    artifact_reference: str | None,
    no_cache: bool,
    repackage: bool = False,
) -> dict[str, Any]:
    """Build a stable local execution plan for workflow dry-run output."""
    normalized_artifact_type = (artifact_type or "").strip().lower()
    normalized_reference = str(artifact_reference or artifact_plan.reference or "")
    source = artifact_plan.source or ("built" if artifact_plan.should_build else "external")
    will_build = artifact_plan.will_build if artifact_plan.will_build is not None else artifact_plan.should_build
    should_publish = artifact_plan.should_publish if artifact_plan.should_publish is not None else artifact_plan.should_build
    will_publish = artifact_plan.will_publish if artifact_plan.will_publish is not None else will_build
    build_reason = _plan_step_reason(
        source=source,
        explicit_ref_option=artifact_plan.explicit_ref_option,
        is_predicted=bool(artifact_plan.should_build and not will_build),
    )
    publish_reason = _plan_step_reason(
        source=source,
        explicit_ref_option=artifact_plan.explicit_ref_option,
        is_predicted=bool(should_publish and not will_publish),
    )
    steps = [
        {"name": "validate_config", "kind": "local", "will_run": True},
        {"name": "package", "kind": "local", "will_run": True},
        {
            "name": "local_build",
            "kind": "local",
            "will_run": bool(will_build),
            "planned": bool(artifact_plan.should_build and not will_build),
            "reason": build_reason,
        },
        {
            "name": "artifact_publish",
            "kind": "remote",
            "will_run": bool(will_publish),
            "planned": bool(should_publish and not will_publish),
            "reason": publish_reason,
        },
        {"name": "deploy_request", "kind": "remote", "will_run": True},
    ]
    return {
        "project_dir": str(project_dir),
        "framework": str(framework or ""),
        "target": str(target or ""),
        "region": str(region or ""),
        "deploy_name": str(deploy_name or ""),
        "artifact_type": normalized_artifact_type,
        "no_cache": bool(no_cache),
        "repackage": bool(repackage),
        "artifact": {
            "should_build": bool(artifact_plan.should_build),
            "will_build": bool(will_build),
            "should_local_build": bool(artifact_plan.should_build),
            "will_local_build": bool(will_build),
            "should_publish": bool(should_publish),
            "will_publish": bool(will_publish),
            "should_clear_metadata": bool(artifact_plan.should_clear_metadata),
            "explicit_ref_option": artifact_plan.explicit_ref_option,
            "source": source,
            "build_dir": str(build_dir or ""),
            "reference": normalized_reference,
            "reference_is_predicted": bool(artifact_plan.reference_is_predicted),
        },
        "steps": steps,
    }


def should_build_artifact(*, target: str, artifact_type: str | None, ks3_path: str | None, image: str | None) -> bool:
    """Whether deploy/launch should build artifacts locally."""
    if target != "serverless":
        return False
    mode = (artifact_type or "Code").strip().lower()
    if mode == "code":
        return not bool(ks3_path)
    if mode == "container":
        return not bool(image)
    return False


def plan_artifact_build(
    *,
    target: str,
    artifact_type: str | None,
    ks3_path: str | None,
    image: str | None,
    no_cache: bool,
    repackage: bool = False,
) -> ArtifactBuildPlan:
    """Plan artifact build behavior and metadata cleanup under cache options."""
    should_build = should_build_artifact(
        target=target,
        artifact_type=artifact_type,
        ks3_path=ks3_path,
        image=image,
    )
    explicit_ref_option = None
    if target == "serverless" and not should_build:
        mode = (artifact_type or "Code").strip().lower()
        if mode == "code" and ks3_path:
            explicit_ref_option = "--ks3-path"
        elif mode == "container" and image:
            explicit_ref_option = "--image"
    return ArtifactBuildPlan(
        should_build=should_build,
        should_clear_metadata=bool((no_cache or repackage) and should_build),
        explicit_ref_option=explicit_ref_option,
        will_build=should_build,
        should_publish=bool(target == "serverless" and should_build),
        will_publish=bool(target == "serverless" and should_build),
        source="external" if explicit_ref_option else ("built" if should_build else None),
    )


def _predict_artifact_reference(
    *,
    target: str,
    artifact_type: str | None,
    deploy_name: str | None,
    region: str | None,
    account_id: str | None,
    ks3_bucket: str | None,
    registry: str | None,
) -> str:
    """Predict the artifact reference that a real build would produce."""
    if target != "serverless":
        return ""

    normalized_artifact_type = (artifact_type or "Code").strip().lower()
    normalized_deploy_name = (deploy_name or "agent").strip() or "agent"
    normalized_region = "cn-beijing-6" if str(region or "").strip() == "pre-online" else str(region or "").strip()

    if normalized_artifact_type == "code":
        bucket = (ks3_bucket or "").strip()
        if not bucket and account_id and normalized_region:
            bucket = f"agentengine-{account_id}-{normalized_region}"
        if not bucket:
            bucket = "<ks3-bucket>"
        return f"ks3://{bucket}/agents/{normalized_deploy_name}/code_<dry-run>.zip"

    if normalized_artifact_type == "container":
        normalized_registry = (registry or "").strip().rstrip("/")
        if not normalized_registry:
            normalized_registry = "<registry>"
        return f"{normalized_registry}/{normalized_deploy_name}:dry-run"

    return ""


def resolve_artifact_build_plan(
    *,
    plan: ArtifactBuildPlan,
    target: str,
    artifact_type: str | None,
    dry_run: bool,
    deploy_name: str | None,
    region: str | None,
    account_id: str | None,
    ks3_bucket: str | None,
    registry: str | None,
    explicit_reference: str | None,
    cached_reference: str | None,
) -> ArtifactBuildPlan:
    """Resolve workflow artifact behavior after package metadata is available."""
    normalized_explicit = str(explicit_reference or "").strip()
    normalized_cached = str(cached_reference or "").strip()

    if normalized_explicit:
        return replace(
            plan,
            should_build=False,
            will_build=False,
            should_publish=False,
            will_publish=False,
            source="external",
            reference=normalized_explicit,
            reference_is_predicted=False,
        )

    if normalized_cached:
        return replace(
            plan,
            should_build=False,
            will_build=False,
            should_publish=False,
            will_publish=False,
            source="cached",
            reference=normalized_cached,
            reference_is_predicted=False,
        )

    if plan.should_build and dry_run:
        return replace(
            plan,
            will_build=False,
            should_publish=bool(target == "serverless"),
            will_publish=False,
            source="planned_build",
            reference=_predict_artifact_reference(
                target=target,
                artifact_type=artifact_type,
                deploy_name=deploy_name,
                region=region,
                account_id=account_id,
                ks3_bucket=ks3_bucket,
                registry=registry,
            ),
            reference_is_predicted=True,
        )

    if plan.should_build:
        return replace(
            plan,
            will_build=True,
            should_publish=bool(target == "serverless"),
            will_publish=bool(target == "serverless"),
            source="built",
            reference=plan.reference,
            reference_is_predicted=False,
        )

    return replace(
        plan,
        should_build=False,
        will_build=False,
        should_publish=False,
        will_publish=False,
        source=plan.source or "external",
        reference=plan.reference or "",
        reference_is_predicted=False,
    )


def _plan_step_reason(*, source: str, explicit_ref_option: str | None, is_predicted: bool) -> str:
    """Explain why a workflow plan step will run, be skipped, or be predicted."""
    if is_predicted:
        return "dry_run_prediction"
    if source == "cached":
        return "cache_hit"
    if source == "external":
        return "explicit_reference" if explicit_ref_option else "external_artifact"
    return ""


def _artifact_source_label(source: str | None) -> str:
    return {
        "planned_build": "预测构建产物",
        "built": "本地构建产物",
        "cached": "缓存制品",
        "external": "外部制品",
    }.get(str(source or "").strip(), str(source or "-"))


def _step_status(step: dict[str, Any]) -> str:
    if step.get("will_run"):
        return "执行"
    if step.get("planned"):
        return "仅计划"
    return "跳过"


def _step_reason_label(reason: str | None) -> str:
    return {
        "dry_run_prediction": "Dry Run 仅展示预期动作",
        "cache_hit": "命中缓存",
        "explicit_reference": "已显式指定外部制品",
        "external_artifact": "使用外部制品",
    }.get(str(reason or "").strip(), "")


def _request_body_summary(body: Any) -> str:
    if body is None:
        return "-"
    if isinstance(body, dict):
        keys = [str(key) for key in body.keys()]
        if not keys:
            return "{}"
        return ", ".join(keys[:8]) + (" ..." if len(keys) > 8 else "")
    if isinstance(body, list):
        return f"list[{len(body)}]"
    return type(body).__name__


def _request_header_summary(headers: Any) -> str:
    if not isinstance(headers, dict):
        return "-"
    keys = [str(key) for key in headers.keys()]
    return ", ".join(keys[:6]) + (" ..." if len(keys) > 6 else "") if keys else "-"


def _summarize_plan_steps(steps: Sequence[dict[str, Any]]) -> tuple[str, str, str]:
    executed = [str(step.get("name") or "-") for step in steps if step.get("will_run")]
    planned = [str(step.get("name") or "-") for step in steps if step.get("planned")]
    skipped = [str(step.get("name") or "-") for step in steps if not step.get("will_run") and not step.get("planned")]
    return (
        ", ".join(executed) or "-",
        ", ".join(planned) or "-",
        ", ".join(skipped) or "-",
    )


def clear_build_metadata(project_dir: Path) -> bool:
    """Clear persisted build metadata, returning whether removal happened."""
    metadata_file = project_dir / ".agentengine" / "build-metadata.json"
    if not metadata_file.exists():
        return False
    metadata_file.unlink()
    return True


def load_cached_artifact_reference(project_dir: Path, artifact_type: str | None) -> str | None:
    """Load a cached artifact reference from build metadata when available."""
    metadata_file = project_dir / ".agentengine" / "build-metadata.json"
    if not metadata_file.exists():
        return None

    try:
        payload = json.loads(metadata_file.read_text(encoding="utf-8"))
    except Exception:
        return None

    metadata = payload.get("metadata") or {}
    normalized_artifact_type = (artifact_type or "Code").strip().lower()
    if normalized_artifact_type == "container":
        value = payload.get("image") or metadata.get("image")
    else:
        value = metadata.get("ks3_path")
    normalized = str(value or "").strip()
    return normalized or None


def print_workflow_header(
    *,
    title: str,
    subtitle: str,
    project_dir: Path,
    target: str | None = None,
    region: str | None = None,
    mode_label: str | None = None,
    mode_value: str | None = None,
    account_id: str | None = None,
    observability: bool | None = None,
) -> None:
    """Print a unified workflow header block."""
    print_title(title, subtitle)
    print_kv("项目目录", str(project_dir))
    if target:
        print_kv("目标", target)
    if region:
        print_kv("区域", region, value_style="#58a6ff")
    if mode_label and mode_value:
        print_kv(mode_label, mode_value)
    if observability is not None:
        print_kv("可观测性", "开启" if observability else "关闭")
    if account_id:
        print_kv("账号 ID", account_id)


def emit_no_cache_hint(*, plan: ArtifactBuildPlan, no_cache: bool) -> None:
    """Emit standardized no-cache behavior hints."""
    if not no_cache:
        return
    if plan.explicit_ref_option:
        print_warn(f"已显式指定 {plan.explicit_ref_option}，--no-cache 不会重建外部制品")


def print_agent_next_steps(agent_ref: str, *, title: str | None = None) -> None:
    """Print canonical next-step hints for deployed agents."""
    steps = [
        f"agentengine agent status --agent {agent_ref}",
        f"agentengine agent invoke --agent {agent_ref}",
    ]
    if title:
        print_next_steps(steps, title=title)
    else:
        print_next_steps(steps)


def build_workflow_result_envelope(
    *,
    action: str,
    result: dict[str, Any],
    hints: Sequence[str] | None = None,
) -> dict[str, Any]:
    return {
        "ok": True,
        "kind": "result",
        "resource": "workflow",
        "action": action,
        "result": dict(result),
        "hints": list(hints or []),
    }


def build_workflow_dry_run_envelope(
    *,
    action: str,
    request: dict[str, Any],
    plan: dict[str, Any] | None = None,
    hints: Sequence[str] | None = None,
) -> dict[str, Any]:
    envelope = {
        "ok": True,
        "kind": "dry_run",
        "resource": "workflow",
        "action": action,
        "request": sanitize_dry_run_request(request),
        "hints": list(hints or []),
    }
    if plan is not None:
        envelope["plan"] = dict(plan)
    return envelope


def render_workflow_result(
    *,
    action: str,
    result: dict[str, Any],
    hints: Sequence[str] | None = None,
) -> None:
    if is_json_output():
        emit_json(
            build_workflow_result_envelope(
                action=action,
                result=result,
                hints=hints,
            )
        )


def render_workflow_dry_run(
    *,
    action: str,
    request: dict[str, Any],
    plan: dict[str, Any] | None = None,
    hints: Sequence[str] | None = None,
) -> None:
    safe_request = sanitize_dry_run_request(request)
    if is_json_output():
        emit_json(
            build_workflow_dry_run_envelope(
                action=action,
                request=safe_request,
                plan=plan,
                hints=hints,
            )
        )
        return

    print_title("Dry Run 计划", action)
    if plan:
        print_kv("项目目录", str(plan.get("project_dir") or "-"))
        print_kv("框架", str(plan.get("framework") or "-"))
        print_kv("目标", str(plan.get("target") or "-"))
        if plan.get("region"):
            print_kv("区域", str(plan.get("region")))
        if plan.get("deploy_name"):
            print_kv("部署名称", str(plan.get("deploy_name")))
        artifact = dict(plan.get("artifact") or {})
        steps = list(plan.get("steps") or [])
        executed, planned, skipped = _summarize_plan_steps(steps)
        print_rule("执行摘要")
        print_info("Dry Run 仅展示执行计划，不会执行真实构建、上传或远端写操作。")
        print_kv("本次执行", executed)
        print_kv("仅计划", planned)
        if skipped != "-":
            print_kv("已跳过", skipped)

        print_rule("本地计划")
        print_kv("制品类型", str(plan.get("artifact_type") or "-"))
        print_kv("需要本地构建", "是" if artifact.get("should_local_build", artifact.get("should_build")) else "否")
        print_kv("会执行本地构建", "是" if artifact.get("will_local_build", artifact.get("will_build")) else "否")
        print_kv("需要发布制品", "是" if artifact.get("should_publish") else "否")
        print_kv("会执行制品发布", "是" if artifact.get("will_publish") else "否")
        if artifact.get("source"):
            print_kv("制品来源", _artifact_source_label(str(artifact.get("source"))))
        if artifact.get("explicit_ref_option"):
            print_kv("外部制品参数", str(artifact.get("explicit_ref_option")))
        if artifact.get("build_dir"):
            print_kv("构建目录", str(artifact.get("build_dir")))
        if artifact.get("reference"):
            print_kv("制品引用", str(artifact.get("reference")))
        if artifact.get("reference_is_predicted"):
            print_kv("引用类型", "预测值")
        print_kv("no-cache", "开启" if plan.get("no_cache") else "关闭")
        if steps:
            print_info("步骤:")
            for step in steps:
                label = str(step.get("name") or "-")
                kind = str(step.get("kind") or "local")
                status = _step_status(step)
                reason = _step_reason_label(str(step.get("reason") or "").strip())
                click.echo(f"  - [{kind}] {label}: {status}")
                if reason:
                    click.echo(f"      原因: {reason}")

    print_rule("远端请求")
    print_kv("请求方法", str(safe_request.get("method", "REQUEST")))
    print_kv("请求地址", str(safe_request.get("url", "")))
    print_kv("请求头", _request_header_summary(safe_request.get("headers")))
    print_kv("请求字段", _request_body_summary(safe_request.get("body")))
    if safe_request.get("curl"):
        print_info("Curl:")
        click.echo(str(safe_request["curl"]))

from pathlib import Path
import json

from ksadk.cli.workflow_common import (
    build_workflow_local_plan,
    clear_build_metadata,
    load_cached_artifact_reference,
    plan_artifact_build,
    resolve_artifact_build_plan,
    should_build_artifact,
)


def test_should_build_artifact_serverless_code_and_container():
    assert should_build_artifact(
        target="serverless",
        artifact_type="Code",
        ks3_path=None,
        image=None,
    ) is True
    assert should_build_artifact(
        target="serverless",
        artifact_type="Code",
        ks3_path="ks3://bucket/object.zip",
        image=None,
    ) is False
    assert should_build_artifact(
        target="serverless",
        artifact_type="Container",
        ks3_path=None,
        image=None,
    ) is True
    assert should_build_artifact(
        target="serverless",
        artifact_type="Container",
        ks3_path=None,
        image="hub.kce.ksyun.com/demo:image",
    ) is False


def test_should_build_artifact_non_serverless_never_builds():
    assert should_build_artifact(
        target="kce",
        artifact_type="Code",
        ks3_path=None,
        image=None,
    ) is False


def test_plan_artifact_build_no_cache_behaviors():
    plan_rebuild = plan_artifact_build(
        target="serverless",
        artifact_type="Code",
        ks3_path=None,
        image=None,
        no_cache=True,
    )
    assert plan_rebuild.should_build is True
    assert plan_rebuild.should_clear_metadata is True
    assert plan_rebuild.explicit_ref_option is None

    plan_external = plan_artifact_build(
        target="serverless",
        artifact_type="Code",
        ks3_path="ks3://bucket/object.zip",
        image=None,
        no_cache=True,
    )
    assert plan_external.should_build is False
    assert plan_external.should_clear_metadata is False
    assert plan_external.explicit_ref_option == "--ks3-path"


def test_plan_artifact_build_repackage_rebuilds_without_clearing_dependency_cache():
    plan = plan_artifact_build(
        target="serverless",
        artifact_type="Code",
        ks3_path=None,
        image=None,
        no_cache=False,
        repackage=True,
    )

    assert plan.should_build is True
    assert plan.should_clear_metadata is True
    assert plan.explicit_ref_option is None


def test_clear_build_metadata(tmp_path: Path):
    metadata_file = tmp_path / ".agentengine" / "build-metadata.json"
    metadata_file.parent.mkdir(parents=True, exist_ok=True)
    metadata_file.write_text("{}", encoding="utf-8")

    assert clear_build_metadata(tmp_path) is True
    assert metadata_file.exists() is False
    assert clear_build_metadata(tmp_path) is False


def test_load_cached_artifact_reference_reads_code_and_container_metadata(tmp_path: Path):
    metadata_file = tmp_path / ".agentengine" / "build-metadata.json"
    metadata_file.parent.mkdir(parents=True, exist_ok=True)
    metadata_file.write_text(
        json.dumps(
            {
                "image": "hub.kce.ksyun.com/demo/demo-agent:latest",
                "metadata": {
                    "ks3_path": "ks3://bucket/agents/demo-agent/code.zip",
                    "image": "hub.kce.ksyun.com/demo/demo-agent:latest",
                },
            }
        ),
        encoding="utf-8",
    )

    assert load_cached_artifact_reference(tmp_path, "Code") == "ks3://bucket/agents/demo-agent/code.zip"
    assert load_cached_artifact_reference(tmp_path, "Container") == "hub.kce.ksyun.com/demo/demo-agent:latest"


def test_resolve_artifact_build_plan_prefers_cached_then_predicted_dry_run():
    base_plan = plan_artifact_build(
        target="serverless",
        artifact_type="Code",
        ks3_path=None,
        image=None,
        no_cache=False,
    )

    cached = resolve_artifact_build_plan(
        plan=base_plan,
        target="serverless",
        artifact_type="Code",
        dry_run=False,
        deploy_name="demo-agent",
        region="cn-beijing-6",
        account_id="2000003485",
        ks3_bucket=None,
        registry=None,
        explicit_reference=None,
        cached_reference="ks3://bucket/agents/demo-agent/cached.zip",
    )
    assert cached.should_build is False
    assert cached.will_build is False
    assert cached.should_publish is False
    assert cached.will_publish is False
    assert cached.source == "cached"
    assert cached.reference == "ks3://bucket/agents/demo-agent/cached.zip"

    predicted = resolve_artifact_build_plan(
        plan=base_plan,
        target="serverless",
        artifact_type="Code",
        dry_run=True,
        deploy_name="demo-agent",
        region="cn-beijing-6",
        account_id="2000003485",
        ks3_bucket=None,
        registry=None,
        explicit_reference=None,
        cached_reference=None,
    )
    assert predicted.should_build is True
    assert predicted.will_build is False
    assert predicted.should_publish is True
    assert predicted.will_publish is False
    assert predicted.source == "planned_build"
    assert predicted.reference_is_predicted is True
    assert predicted.reference == "ks3://agentengine-2000003485-cn-beijing-6/agents/demo-agent/code_<dry-run>.zip"


def test_build_workflow_local_plan_splits_local_build_and_artifact_publish_steps():
    base_plan = plan_artifact_build(
        target="serverless",
        artifact_type="Code",
        ks3_path=None,
        image=None,
        no_cache=False,
    )
    predicted = resolve_artifact_build_plan(
        plan=base_plan,
        target="serverless",
        artifact_type="Code",
        dry_run=True,
        deploy_name="demo-agent",
        region="cn-beijing-6",
        account_id="2000003485",
        ks3_bucket=None,
        registry=None,
        explicit_reference=None,
        cached_reference=None,
    )

    plan = build_workflow_local_plan(
        project_dir=Path("/tmp/demo-agent"),
        framework="langgraph",
        target="serverless",
        region="cn-beijing-6",
        deploy_name="demo-agent",
        artifact_type="Code",
        artifact_plan=predicted,
        build_dir="/tmp/demo-agent/.agentengine/build",
        artifact_reference=predicted.reference,
        no_cache=False,
    )

    assert [step["name"] for step in plan["steps"]] == [
        "validate_config",
        "package",
        "local_build",
        "artifact_publish",
        "deploy_request",
    ]
    assert plan["steps"][2]["kind"] == "local"
    assert plan["steps"][2]["planned"] is True
    assert plan["steps"][2]["reason"] == "dry_run_prediction"
    assert plan["steps"][3]["kind"] == "remote"
    assert plan["steps"][3]["planned"] is True
    assert plan["steps"][3]["reason"] == "dry_run_prediction"
    assert plan["artifact"]["should_local_build"] is True
    assert plan["artifact"]["will_local_build"] is False
    assert plan["artifact"]["should_publish"] is True
    assert plan["artifact"]["will_publish"] is False

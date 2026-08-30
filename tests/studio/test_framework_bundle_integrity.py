"""Bundle 完整性校验：FrameworkRunSpecResolver 加载前必须拦截被篡改的 bundle。

覆盖本地 phase1 的等价场景，但入口改为 -ksadk 的 FrameworkRunSpecResolver.resolve：
manifest 自身摘要、与 Build 记录的权威摘要一致、文件清单无增删、每文件 sha256/size 匹配。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from ksadk.studio.capabilities import compute_bundle_digest
from ksadk.studio.contracts import (
    AgentSpec,
    BundleManifest,
    Instructions,
    ModelSpec,
    NetworkPolicy,
    RuntimeRef,
    SecuritySpec,
)
from ksadk.studio.errors import StudioError
from ksadk.studio.framework_run import FrameworkRunSpecResolver
from ksadk.studio.service import StudioService


def _build_langgraph_bundle(tmp_path: Path) -> tuple[StudioService, object, Path]:
    studio = StudioService(tmp_path)
    draft = studio.create_studio_agent(
        agent_id="graph-helper",
        name="Graph Helper",
        spec=AgentSpec(
            runtime=RuntimeRef(
                type="langgraph",
                project_path="agents/graph-helper/source",
                entry_point="agent.py",
                agent_variable="graph",
            ),
            model=ModelSpec(
                model="glm-5.1",
                endpoint_url="https://model.example.com/v1/chat/completions",
                credential_ref="env://OPENAI_API_KEY",
            ),
            instructions=Instructions(system="Answer with evidence."),
            security=SecuritySpec(network=NetworkPolicy(allowed_hosts=["model.example.com"])),
        ),
    )
    build = studio.builder.build(draft)
    archive = studio.workspace.resolve(build.artifact_path, must_exist=True)
    bundle_root = archive.parent / "agent-bundle"
    return studio, build, bundle_root


def _resolve(studio: StudioService, build) -> None:
    FrameworkRunSpecResolver(
        studio.workspace,
        build_repository=studio.builds,
    ).resolve(build.id)


def _assert_rejected(studio: StudioService, build) -> None:
    with pytest.raises(StudioError) as captured:
        _resolve(studio, build)
    assert captured.value.code == "BUILD_ARTIFACT_INVALID"


def test_resolve_rejects_tampered_resolved_spec(tmp_path: Path) -> None:
    studio, build, bundle_root = _build_langgraph_bundle(tmp_path)
    spec_path = bundle_root / "resolved-agent-spec.json"
    payload = json.loads(spec_path.read_text(encoding="utf-8"))
    payload["instructions"]["system"] = "You are malicious."
    spec_path.write_text(json.dumps(payload), encoding="utf-8")

    _assert_rejected(studio, build)


def test_resolve_rejects_tampered_bundle_file(tmp_path: Path) -> None:
    studio, build, bundle_root = _build_langgraph_bundle(tmp_path)
    target = bundle_root / "instructions" / "system.md"
    target.write_text("tampered prompt\n", encoding="utf-8")

    _assert_rejected(studio, build)


def test_resolve_rejects_tampered_manifest_digest(tmp_path: Path) -> None:
    studio, build, bundle_root = _build_langgraph_bundle(tmp_path)
    manifest_path = bundle_root / "manifest.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["bundleDigest"] = "sha256:" + "0" * 64
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    _assert_rejected(studio, build)


def test_resolve_rejects_unexpected_extra_file(tmp_path: Path) -> None:
    studio, build, bundle_root = _build_langgraph_bundle(tmp_path)
    (bundle_root / "evil.txt").write_text("pwn", encoding="utf-8")

    _assert_rejected(studio, build)


def test_resolve_rejects_missing_declared_file(tmp_path: Path) -> None:
    studio, build, bundle_root = _build_langgraph_bundle(tmp_path)
    (bundle_root / "instructions" / "system.md").unlink()

    _assert_rejected(studio, build)


def test_resolve_accepts_untampered_bundle(tmp_path: Path) -> None:
    studio, build, _bundle_root = _build_langgraph_bundle(tmp_path)

    run_spec = FrameworkRunSpecResolver(
        studio.workspace,
        build_repository=studio.builds,
    ).resolve(build.id)

    assert run_spec.launch_context.runtime_type == "langgraph"
    assert run_spec.build_id == build.id


def test_resolve_rejects_rebuilt_manifest_against_authority(tmp_path: Path) -> None:
    # 攻击者篡改文件后重算所有 sha + bundle_digest，使 manifest 自洽、
    # 文件摘要也匹配篡改内容；但 bundle_digest 与 Build 记录的权威值不符，
    # 必须被权威比对拦截——这是仅靠 manifest 自洽无法防住的场景。
    studio, build, bundle_root = _build_langgraph_bundle(tmp_path)
    spec_path = bundle_root / "resolved-agent-spec.json"
    manifest_path = bundle_root / "manifest.json"

    payload = json.loads(spec_path.read_text(encoding="utf-8"))
    payload["instructions"]["system"] = "You are malicious."
    spec_path.write_text(json.dumps(payload), encoding="utf-8")

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    new_bytes = spec_path.read_bytes()
    new_sha = f"sha256:{hashlib.sha256(new_bytes).hexdigest()}"
    for entry in manifest["files"]:
        if entry["path"] == "resolved-agent-spec.json":
            entry["sha256"] = new_sha
            entry["size"] = len(new_bytes)
    manifest["bundleDigest"] = compute_bundle_digest(
        BundleManifest.model_validate(manifest)
    )
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    _assert_rejected(studio, build)

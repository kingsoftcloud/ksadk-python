from __future__ import annotations

from pathlib import Path

from ksadk.harness.release_readiness import ReleaseEvidence, release_readiness
from ksadk.harness.skill_matrix import run_skill_matrix
from ksadk.harness.spec import (
    CapabilityBinding,
    CapabilityBindings,
    HarnessSpec,
    ModelBinding,
    PromptSpec,
)


def _spec(ref: str, *, required: bool = True, load_policy: str = "on_demand") -> HarnessSpec:
    return HarnessSpec(
        agent_revision_ref="agent-revision://a@1",
        model=ModelBinding(profile_ref="model-profile://m@1.0.0"),
        prompt=PromptSpec(instructions="test"),
        capabilities=CapabilityBindings(
            skill_bindings=(
                CapabilityBinding(
                    capability_ref=ref,
                    required=required,
                    load_policy=load_policy,
                ),
            )
        ),
    )


def _write_skill(root: Path) -> None:
    root.mkdir(parents=True)
    (root / "SKILL.md").write_text(
        "---\nname: finance\ndescription: 财务分析\n---\n私有操作正文。\n",
        encoding="utf-8",
    )


def test_valid_skill_is_ready_without_leaking_content_or_absolute_path(tmp_path: Path):
    root = tmp_path / "finance@1.0.0"
    _write_skill(root)

    payload = run_skill_matrix(_spec("skill://finance@1.0.0"), local_dir=tmp_path).to_dict()

    assert payload["status"] == "ready"
    assert payload["counts"] == {"ready": 1, "warning": 0, "blocked": 0}
    assert payload["rows"][0]["capabilities"] == {
        "resolved": True,
        "manifestValid": True,
        "progressiveDisclosure": True,
        "name": "finance",
    }
    assert "私有操作正文" not in str(payload)
    assert str(tmp_path) not in str(payload)


def test_required_missing_skill_blocks_release(tmp_path: Path):
    report = run_skill_matrix(_spec("skill://finance@1.0.0"), local_dir=tmp_path)
    readiness = release_readiness(
        {"status": "ready"},
        (ReleaseEvidence("skills", "skill", report),),
    )

    assert report.status == "blocked"
    assert readiness["deployable"] is False
    assert readiness["fixes"][0]["action"].startswith("固定 Skill 版本")


def test_optional_missing_skill_warns_instead_of_blocking(tmp_path: Path):
    report = run_skill_matrix(
        _spec("skill://finance@1.0.0", required=False), local_dir=tmp_path
    ).to_dict()

    assert report["status"] == "warning"
    assert report["rows"][0]["status"] == "warning"


def test_explicit_skill_is_ready_without_resolving_runtime_content():
    report = run_skill_matrix(
        _spec("skill://finance@1.0.0", load_policy="explicit")
    ).to_dict()

    assert report["status"] == "ready"
    assert report["rows"][0]["capabilities"]["resolved"] is False
    assert report["rows"][0]["findings"][1] == {
        "rule": "skill.runtime.visibility",
        "status": "passed",
        "detail": "explicit binding is hidden from the model loop",
    }


def test_empty_skill_bindings_are_ready():
    spec = HarnessSpec(
        agent_revision_ref="agent-revision://a@1",
        model=ModelBinding(profile_ref="model-profile://m@1.0.0"),
        prompt=PromptSpec(instructions="test"),
    )

    assert run_skill_matrix(spec).to_dict()["status"] == "ready"

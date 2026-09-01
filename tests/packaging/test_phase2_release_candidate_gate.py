from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.phase2_release_candidate_gate import (
    ReleaseCandidateGateError,
    build_release_candidate_report,
)
from scripts.phase2_release_preflight import PHASE2_E2E_STATUS_KEYS

COMMIT = "a" * 40
WEB_COMMIT = "b" * 40
INTEGRITY = "sha512-" + ("A" * 86) + "=="
IMAGE = "hub.example.invalid/agentengine-hosted-ui@sha256:" + ("c" * 64)


def _write(path: Path, payload: dict) -> Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _evidence(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    local = {
        "schemaVersion": 2,
        "phase": "phase2",
        "scope": "local-source-and-package",
        "overallStatus": "incomplete",
        "localStatus": "passed",
        "releaseStatus": "not_evaluated",
        "sourceCommit": COMMIT,
        "contractDigest": "sha256:" + ("d" * 64),
        "artifacts": {
            "wheel": {"file": "ksadk-0.8.3-py3-none-any.whl", "sha256": "sha256:" + ("e" * 64)},
            "sdist": {"file": "ksadk-0.8.3.tar.gz", "sha256": "sha256:" + ("f" * 64)},
        },
        "e2e": {name: "passed" for name in PHASE2_E2E_STATUS_KEYS},
    }
    web = {
        "schemaVersion": 1,
        "status": "published",
        "registry": "https://registry.npmjs.org",
        "package": "@kingsoftcloud/ksadk-web",
        "version": "0.3.4",
        "npmIntegrity": INTEGRITY,
        "sourceCommit": WEB_COMMIT,
    }
    deployment = {
        "schemaVersion": 1,
        "environment": "preproduction",
        "hostedUiImage": IMAGE,
        "helmRevision": 60,
        "webPackage": {
            "package": "@kingsoftcloud/ksadk-web",
            "version": "0.3.4",
            "npmIntegrity": INTEGRITY,
        },
    }
    scenario = {
        "status": "passed",
        "agentId": "ar-example",
        "sessionId": "session-example",
        "turns": 2,
        "streamChunks": 3,
        "duplicateItems": 0,
        "surfaces": ["studio", "hosted-ui"],
    }
    preprod = {
        "schemaVersion": 1,
        "environment": "preproduction",
        "sourceCommit": COMMIT,
        "hostedUiImage": IMAGE,
        "webPackage": {
            "package": "@kingsoftcloud/ksadk-web",
            "version": "0.3.4",
            "npmIntegrity": INTEGRITY,
        },
        "scenarios": {
            "studioCreatedAgent": {**scenario, "cleanupStatus": "deleted"},
            "historical082Agent": {
                **scenario,
                "agentId": "ar-historical",
                "sessionId": "session-historical",
                "cleanupStatus": "preserved",
            },
        },
    }
    return (
        _write(tmp_path / "local.json", local),
        _write(tmp_path / "web.json", web),
        _write(tmp_path / "deployment.json", deployment),
        _write(tmp_path / "preprod.json", preprod),
    )


def _build(paths: tuple[Path, Path, Path, Path], *, commit: str = COMMIT) -> dict:
    return build_release_candidate_report(
        expected_commit=commit,
        local_path=paths[0],
        web_path=paths[1],
        deployment_path=paths[2],
        preprod_path=paths[3],
    )


def test_final_gate_binds_every_release_surface(tmp_path: Path) -> None:
    paths = _evidence(tmp_path)
    report = _build(paths)

    assert report["overallStatus"] == "passed"
    assert report["sourceCommit"] == COMMIT
    assert report["webPackage"]["version"] == "0.3.4"
    assert report["webPackage"]["npmIntegrity"] == INTEGRITY
    assert report["hostedUi"]["image"] == IMAGE
    assert report["scenarios"] == {
        "studioCreatedAgent": "passed",
        "historical082Agent": "passed",
    }
    assert all(value.startswith("sha256:") for value in report["inputs"].values())


@pytest.mark.parametrize(
    ("index", "path", "value", "message"),
    [
        (0, ("sourceCommit",), "c" * 40, "final commit"),
        (1, ("status",), "not_published", "not published"),
        (2, ("hostedUiImage",), "latest", "digest-pinned"),
        (3, ("sourceCommit",), "c" * 40, "final commit"),
        (3, ("scenarios", "studioCreatedAgent", "streamChunks"), 1, "streaming"),
        (3, ("scenarios", "historical082Agent", "duplicateItems"), 1, "duplicates"),
    ],
)
def test_final_gate_rejects_unbound_or_weak_evidence(
    tmp_path: Path,
    index: int,
    path: tuple[str, ...],
    value: object,
    message: str,
) -> None:
    paths = _evidence(tmp_path)
    payload = json.loads(paths[index].read_text(encoding="utf-8"))
    target = payload
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    _write(paths[index], payload)

    with pytest.raises(ReleaseCandidateGateError, match=message):
        _build(paths)


def test_final_gate_rejects_secret_shaped_evidence(tmp_path: Path) -> None:
    paths = _evidence(tmp_path)
    payload = json.loads(paths[3].read_text(encoding="utf-8"))
    payload["accessToken"] = "must-not-appear"
    _write(paths[3], payload)

    with pytest.raises(ReleaseCandidateGateError, match="secret-shaped evidence key"):
        _build(paths)


def test_final_gate_rejects_the_previous_web_release(tmp_path: Path) -> None:
    paths = _evidence(tmp_path)
    for index in (1, 2, 3):
        payload = json.loads(paths[index].read_text(encoding="utf-8"))
        if index == 1:
            payload["version"] = "0.3.3"
        else:
            payload["webPackage"]["version"] = "0.3.3"
        _write(paths[index], payload)

    with pytest.raises(ReleaseCandidateGateError, match="Web package identity"):
        _build(paths)

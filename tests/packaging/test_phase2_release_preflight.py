from __future__ import annotations

import io
import json
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

from ksadk.version import VERSION
from scripts.phase2_release_preflight import (
    BROWSER_GATES,
    COMPATIBILITY_TESTS,
    CREDENTIAL_FREE_NATIVE_TESTS,
    MANAGED_DSH_TOOLCHAIN_TESTS,
    PHASE2_E2E_STATUS_KEYS,
    ROOT,
    Phase2PreflightError,
    build_phase2_evidence_report,
    phase2_contract_digest,
    run_release_test_gates,
    validate_clean_artifact_installations,
    validate_distribution_archives,
    validate_generated_static_tracking_policy,
    validate_phase2_evidence_report,
    write_phase2_evidence_report,
)

SOURCE_COMMIT = "a" * 40


def test_preflight_executes_phase2_release_journeys() -> None:
    assert "tests/compat/test_release_082_asset_compat.py" in COMPATIBILITY_TESTS
    assert "tests/compat/test_phase2_legacy_compat.py" in COMPATIBILITY_TESTS
    assert "tests/e2e/test_codex_plugin_bridge_e2e.py" in CREDENTIAL_FREE_NATIVE_TESTS
    assert "tests/e2e/test_codex_provider_app_server_e2e.py" in CREDENTIAL_FREE_NATIVE_TESTS
    assert "tests/e2e/test_codex_subagent_provider_e2e.py" in CREDENTIAL_FREE_NATIVE_TESTS
    assert MANAGED_DSH_TOOLCHAIN_TESTS == (
        "tests/e2e/test_dsh_managed_toolchain_e2e.py",
        "tests/plugins/test_dsh_node_provider_e2e.py",
    )
    assert "tests/studio/e2e/dsh_client_bundle_browser_e2e.py" in BROWSER_GATES
    assert "tests/studio/e2e/scheduler_browser_e2e.py" in BROWSER_GATES
    assert "tests/studio/e2e/scheduler_harness_browser_e2e.py" in BROWSER_GATES
    assert "tests/studio/e2e/scheduler_fault_matrix_browser_e2e.py" in BROWSER_GATES
    assert "tests/studio/e2e/conversation_reconnect_browser_e2e.py" in BROWSER_GATES
    assert "tests/studio/e2e/conversation_items_browser_e2e.py" in BROWSER_GATES


def test_preflight_references_only_existing_tests() -> None:
    for path in (
        *COMPATIBILITY_TESTS,
        *CREDENTIAL_FREE_NATIVE_TESTS,
        *MANAGED_DSH_TOOLCHAIN_TESTS,
        *BROWSER_GATES,
    ):
        assert (Path(__file__).resolve().parents[2] / path).is_file(), path


def test_preflight_enables_real_managed_dsh_toolchain_gate(monkeypatch) -> None:
    calls: list[tuple[tuple[str, ...], dict[str, str] | None]] = []

    def record(command, *, environment=None) -> None:
        calls.append((tuple(command), environment))

    monkeypatch.setattr("scripts.phase2_release_preflight._run", record)
    statuses = run_release_test_gates()

    toolchain_calls = [
        (command, environment)
        for command, environment in calls
        if any(path in command for path in MANAGED_DSH_TOOLCHAIN_TESTS)
    ]
    assert toolchain_calls == [
        (
            (
                sys.executable,
                "-m",
                "pytest",
                "-q",
                "tests/e2e/test_dsh_managed_toolchain_e2e.py",
                "tests/plugins/test_dsh_node_provider_e2e.py",
            ),
            {"KSADK_DSH_TOOLCHAIN_E2E": "1"},
        )
    ]
    assert statuses == {
        "compatibilityRegression": "passed",
        "codexNative": "passed",
        "managedDshToolchain": "passed",
        "studioBrowser": "passed",
    }


def test_release_check_builds_provenance_bound_pair_before_phase2_gate() -> None:
    root = Path(__file__).resolve().parents[2]
    workflow = (root / ".github/workflows/release-check.yml").read_text(encoding="utf-8")

    assert "uv sync --extra all" in workflow
    assert "playwright install --with-deps chromium" in workflow
    assert 'test -z "$(git status --porcelain --untracked-files=all)"' in workflow
    assert "rm -rf dist" in workflow
    provenance = workflow.index("scripts/write_build_provenance.py")
    build = workflow.index("uv build --out-dir dist")
    preflight = workflow.index("scripts/phase2_release_preflight.py --dist-dir dist")
    assert provenance < build < preflight


def _provenance(
    *,
    commit: str = SOURCE_COMMIT,
    tree: str = "clean",
) -> bytes:
    return json.dumps(
        {
            "schemaVersion": 1,
            "version": VERSION,
            "sourceCommit": commit,
            "sourceTree": tree,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


STATIC_FILES = {
    "ksadk/_build_provenance.json": _provenance(),
    "ksadk/server/static/index.html": b"<html>Web</html>",
    "ksadk/server/static/assets/app.js": b"web",
    "ksadk/studio/static/index.html": b'<div id="root"></div>',
    "ksadk/studio/static/assets/app.js": b"studio",
}


def _write_wheel(path: Path, files: dict[str, bytes]) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)


def _write_sdist(path: Path, files: dict[str, bytes]) -> None:
    with tarfile.open(path, "w:gz") as archive:
        for name, content in files.items():
            payload = io.BytesIO(content)
            info = tarfile.TarInfo(f"ksadk-{VERSION}/{name}")
            info.size = len(content)
            archive.addfile(info, payload)


def _write_pair(dist_dir: Path, files: dict[str, bytes]) -> None:
    dist_dir.mkdir()
    _write_wheel(dist_dir / f"ksadk-{VERSION}-py3-none-any.whl", files)
    _write_sdist(dist_dir / f"ksadk-{VERSION}.tar.gz", files)


def test_artifact_gate_requires_static_in_wheel_and_sdist(tmp_path: Path) -> None:
    dist_dir = tmp_path / "dist"
    _write_pair(dist_dir, STATIC_FILES)

    artifacts = validate_distribution_archives(
        dist_dir,
        expected_source_commit=SOURCE_COMMIT,
    )

    assert len(artifacts) == 2


def test_artifact_gate_rejects_missing_studio_static(tmp_path: Path) -> None:
    dist_dir = tmp_path / "dist"
    files = {
        name: content
        for name, content in STATIC_FILES.items()
        if not name.startswith("ksadk/studio/static/")
    }
    _write_pair(dist_dir, files)

    with pytest.raises(Phase2PreflightError, match="missing static"):
        validate_distribution_archives(dist_dir, expected_source_commit=SOURCE_COMMIT)


def test_artifact_gate_rejects_editable_frontend_sources(tmp_path: Path) -> None:
    dist_dir = tmp_path / "dist"
    files = {
        **STATIC_FILES,
        "ksadk/studio/react-ui/src/main.tsx": b"export default null",
    }
    _write_pair(dist_dir, files)

    with pytest.raises(Phase2PreflightError, match="frontend source leaked"):
        validate_distribution_archives(dist_dir, expected_source_commit=SOURCE_COMMIT)


def test_artifact_gate_rejects_vendored_node_modules(tmp_path: Path) -> None:
    dist_dir = tmp_path / "dist"
    files = {
        **STATIC_FILES,
        "ksadk/studio/static/node_modules/react/index.js": b"module.exports = {}",
    }
    _write_pair(dist_dir, files)

    with pytest.raises(Phase2PreflightError, match="frontend source leaked"):
        validate_distribution_archives(dist_dir, expected_source_commit=SOURCE_COMMIT)


def test_artifact_gate_rejects_stale_dist_residue(tmp_path: Path) -> None:
    dist_dir = tmp_path / "dist"
    _write_pair(dist_dir, STATIC_FILES)
    _write_wheel(dist_dir / "ksadk-0.8.1-py3-none-any.whl", STATIC_FILES)

    with pytest.raises(Phase2PreflightError, match="must be clean"):
        validate_distribution_archives(dist_dir, expected_source_commit=SOURCE_COMMIT)


def test_artifact_gate_does_not_accept_a_version_prefix_collision(tmp_path: Path) -> None:
    dist_dir = tmp_path / "dist"
    dist_dir.mkdir()
    _write_wheel(dist_dir / "ksadk-0.8.20-py3-none-any.whl", STATIC_FILES)
    _write_sdist(dist_dir / "ksadk-0.8.20.tar.gz", STATIC_FILES)

    with pytest.raises(Phase2PreflightError, match="stale distribution artifact"):
        validate_distribution_archives(dist_dir, expected_source_commit=SOURCE_COMMIT)


def test_artifact_gate_rejects_a_dirty_source_build(tmp_path: Path) -> None:
    dist_dir = tmp_path / "dist"
    _write_pair(
        dist_dir,
        {**STATIC_FILES, "ksadk/_build_provenance.json": _provenance(tree="dirty")},
    )

    with pytest.raises(Phase2PreflightError, match="dirty source tree"):
        validate_distribution_archives(dist_dir, expected_source_commit=SOURCE_COMMIT)


def test_artifact_gate_rejects_a_stale_source_commit(tmp_path: Path) -> None:
    dist_dir = tmp_path / "dist"
    _write_pair(
        dist_dir,
        {
            **STATIC_FILES,
            "ksadk/_build_provenance.json": _provenance(commit="b" * 40),
        },
    )

    with pytest.raises(Phase2PreflightError, match="does not match checked-out commit"):
        validate_distribution_archives(dist_dir, expected_source_commit=SOURCE_COMMIT)


def test_artifact_gate_rejects_mismatched_wheel_and_sdist_provenance(
    tmp_path: Path,
) -> None:
    dist_dir = tmp_path / "dist"
    dist_dir.mkdir()
    _write_wheel(dist_dir / f"ksadk-{VERSION}-py3-none-any.whl", STATIC_FILES)
    _write_sdist(
        dist_dir / f"ksadk-{VERSION}.tar.gz",
        {
            **STATIC_FILES,
            "ksadk/_build_provenance.json": json.dumps(
                {
                    "sourceCommit": SOURCE_COMMIT,
                    "sourceTree": "clean",
                    "schemaVersion": 1,
                    "version": VERSION,
                    "extra": "not-identical",
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode(),
        },
    )

    with pytest.raises(Phase2PreflightError, match="identical build provenance"):
        validate_distribution_archives(dist_dir, expected_source_commit=SOURCE_COMMIT)


def test_generated_static_payload_is_not_tracked() -> None:
    validate_generated_static_tracking_policy(
        public_export=(ROOT / "export-manifest.json").is_file()
    )


def test_clean_public_export_requires_tracked_compiled_static(monkeypatch, tmp_path: Path) -> None:
    (tmp_path / "export-manifest.json").write_text(
        json.dumps({"sourceCommit": SOURCE_COMMIT, "sourceTree": "clean"}),
        encoding="utf-8",
    )
    tracked = "\n".join(
        (
            "ksadk/server/static/index.html",
            "ksadk/server/static/assets/server.js",
            "ksadk/studio/static/index.html",
            "ksadk/studio/static/assets/studio.js",
        )
    )

    monkeypatch.setattr(
        "scripts.phase2_release_preflight.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args=args[0], returncode=0, stdout=tracked, stderr=""
        ),
    )

    validate_generated_static_tracking_policy(tmp_path, public_export=True)


def test_git_free_clean_export_uses_attested_source_identity(tmp_path: Path) -> None:
    (tmp_path / "export-manifest.json").write_text(
        json.dumps({"sourceCommit": SOURCE_COMMIT, "sourceTree": "clean"}),
        encoding="utf-8",
    )

    from scripts.phase2_release_preflight import _current_source_commit

    assert _current_source_commit(tmp_path) == SOURCE_COMMIT
    validate_generated_static_tracking_policy(tmp_path, public_export=True)


def test_clean_install_gate_installs_wheel_and_rebuilt_sdist_wheel_separately(
    tmp_path: Path,
) -> None:
    dist_dir = tmp_path / "dist"
    _write_pair(dist_dir, STATIC_FILES)
    artifacts = validate_distribution_archives(
        dist_dir,
        expected_source_commit=SOURCE_COMMIT,
    )
    calls: list[tuple[tuple[str, ...], Path]] = []

    def record(command, *, environment=None, cwd=None) -> None:
        del environment
        normalized = tuple(str(item) for item in command)
        calls.append((normalized, Path(cwd)))
        if "wheel" in normalized and "--wheel-dir" in normalized:
            wheel_dir = Path(normalized[normalized.index("--wheel-dir") + 1])
            wheel_dir.mkdir(parents=True, exist_ok=True)
            (wheel_dir / f"ksadk-{VERSION}-py3-none-any.whl").write_bytes(b"rebuilt")

    statuses = validate_clean_artifact_installations(artifacts, runner=record)

    assert statuses == {
        "cleanWheelInstall": "passed",
        "cleanSdistInstall": "passed",
    }
    commands = [command for command, _cwd in calls]
    venv_calls = [command for command in commands if command[1:3] == ("-m", "venv")]
    assert len(venv_calls) == 2
    sdist = str(dist_dir / f"ksadk-{VERSION}.tar.gz")
    sdist_build = next(
        index
        for index, command in enumerate(commands)
        if "wheel" in command and sdist in command
    )
    rebuilt_install = next(
        index
        for index, command in enumerate(commands)
        if "install" in command
        and any(item.endswith(".whl") and item != str(artifacts[0]) for item in command)
    )
    assert sdist_build < rebuilt_install
    assert sum(command[-3:] == ("plugin", "toolchain", "--help") for command in commands) == 2
    smoke_calls = [command for command in commands if "-c" in command]
    assert len(smoke_calls) == 2
    assert len({cwd for _command, cwd in calls if cwd is not None}) >= 2


def _passed_e2e_statuses() -> dict[str, str]:
    return {name: "passed" for name in PHASE2_E2E_STATUS_KEYS}


def test_phase2_evidence_report_binds_contract_commit_artifacts_and_e2e(
    tmp_path: Path,
) -> None:
    dist_dir = tmp_path / "dist"
    _write_pair(dist_dir, STATIC_FILES)
    artifacts = validate_distribution_archives(
        dist_dir,
        expected_source_commit=SOURCE_COMMIT,
    )
    contract_digest = phase2_contract_digest()
    report = build_phase2_evidence_report(
        artifacts,
        source_commit=SOURCE_COMMIT,
        contract_digest=contract_digest,
        e2e_statuses=_passed_e2e_statuses(),
    )

    validate_phase2_evidence_report(
        report,
        artifacts=artifacts,
        source_commit=SOURCE_COMMIT,
        contract_digest=contract_digest,
        require_complete=True,
    )
    assert report["overallStatus"] == "incomplete"
    assert report["localStatus"] == "passed"
    assert report["releaseStatus"] == "not_evaluated"
    assert report["contractDigest"] == contract_digest
    assert report["sourceCommit"] == SOURCE_COMMIT
    assert set(report["artifacts"]) == {"wheel", "sdist"}
    assert all(item["sha256"].startswith("sha256:") for item in report["artifacts"].values())

    output = tmp_path / "phase2-evidence.json"
    write_phase2_evidence_report(output, report)
    assert json.loads(output.read_text(encoding="utf-8")) == report


def test_phase2_evidence_report_rejects_artifact_tampering(tmp_path: Path) -> None:
    dist_dir = tmp_path / "dist"
    _write_pair(dist_dir, STATIC_FILES)
    artifacts = validate_distribution_archives(
        dist_dir,
        expected_source_commit=SOURCE_COMMIT,
    )
    contract_digest = phase2_contract_digest()
    report = build_phase2_evidence_report(
        artifacts,
        source_commit=SOURCE_COMMIT,
        contract_digest=contract_digest,
        e2e_statuses=_passed_e2e_statuses(),
    )
    artifacts[0].write_bytes(artifacts[0].read_bytes() + b"tampered")

    with pytest.raises(Phase2PreflightError, match="artifact digest"):
        validate_phase2_evidence_report(
            report,
            artifacts=artifacts,
            source_commit=SOURCE_COMMIT,
            contract_digest=contract_digest,
            require_complete=True,
        )


def test_phase2_evidence_report_cannot_be_complete_when_a_key_e2e_was_not_run(
    tmp_path: Path,
) -> None:
    dist_dir = tmp_path / "dist"
    _write_pair(dist_dir, STATIC_FILES)
    artifacts = validate_distribution_archives(
        dist_dir,
        expected_source_commit=SOURCE_COMMIT,
    )
    contract_digest = phase2_contract_digest()
    statuses = _passed_e2e_statuses()
    statuses["studioBrowser"] = "not_run"
    report = build_phase2_evidence_report(
        artifacts,
        source_commit=SOURCE_COMMIT,
        contract_digest=contract_digest,
        e2e_statuses=statuses,
    )

    assert report["localStatus"] == "incomplete"
    with pytest.raises(Phase2PreflightError, match="local status is incomplete"):
        validate_phase2_evidence_report(
            report,
            artifacts=artifacts,
            source_commit=SOURCE_COMMIT,
            contract_digest=contract_digest,
            require_complete=True,
        )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("sourceCommit", "b" * 40, "source commit"),
        ("contractDigest", f"sha256:{'b' * 64}", "contract digest"),
        ("localStatus", "incomplete", "local status"),
        ("overallStatus", "passed", "overall release completion"),
    ],
)
def test_phase2_evidence_report_rejects_unbound_or_incomplete_claims(
    tmp_path: Path,
    field: str,
    value: str,
    message: str,
) -> None:
    dist_dir = tmp_path / "dist"
    _write_pair(dist_dir, STATIC_FILES)
    artifacts = validate_distribution_archives(
        dist_dir,
        expected_source_commit=SOURCE_COMMIT,
    )
    contract_digest = phase2_contract_digest()
    report = build_phase2_evidence_report(
        artifacts,
        source_commit=SOURCE_COMMIT,
        contract_digest=contract_digest,
        e2e_statuses=_passed_e2e_statuses(),
    )
    report[field] = value

    with pytest.raises(Phase2PreflightError, match=message):
        validate_phase2_evidence_report(
            report,
            artifacts=artifacts,
            source_commit=SOURCE_COMMIT,
            contract_digest=contract_digest,
            require_complete=True,
        )

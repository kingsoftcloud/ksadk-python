import hashlib
import json
import subprocess
import sys
from types import SimpleNamespace

import pytest

from ksadk.sandbox.base import SandboxCommandResult
from ksadk.skills.runtime.backends.e2b import _SANDBOX_VERSION_PROBE, E2BSkillRuntimeBackend


@pytest.mark.parametrize(
    "module_source",
    [
        "# Old build without pinned support\n",
        "PINNED_PACKAGE_PROTOCOL_VERSION = 1\nARTIFACT_DELIVERY_PROTOCOL_VERSION = 1\n",
        "raise ImportError('missing dependency')\n",
    ],
)
def test_probe_reports_version_even_when_protocol_import_would_fail(tmp_path, module_source):
    for folder in ("ksadk", "ksadk/skills", "ksadk/skills/runtime"):
        directory = tmp_path / folder
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "__init__.py").write_text("")
    agent = tmp_path / "ksadk/skills/runtime/agent.py"
    agent.write_text(module_source)
    metadata = tmp_path / "ksadk-0.8.4.dist-info"
    metadata.mkdir()
    (metadata / "METADATA").write_text("Metadata-Version: 2.1\nName: ksadk\nVersion: 0.8.4\n")
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            "import sys; sys.path.insert(0, sys.argv[1]);\n" + _SANDBOX_VERSION_PROBE,
            str(tmp_path),
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    report = json.loads(result.stdout)
    assert report["ksadk_version"] == "0.8.4"
    assert report["agent_file"] == str(agent)
    assert report["agent_sha256"] == hashlib.sha256(agent.read_bytes()).hexdigest()
    if "raise ImportError" in module_source:
        assert report["agent_import_error"] == "ImportError"
    elif "PINNED_PACKAGE" in module_source:
        assert report["pinned_protocol"] == report["artifact_protocol"] == 1
    else:
        assert report["pinned_protocol"] is None
        assert report["artifact_protocol"] is None


@pytest.mark.parametrize("diagnostic_fails", [False, True])
def test_version_diagnostic_preserves_original_failure_and_cleanup(caplog, diagnostic_fails):
    calls = []
    killed = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        if len(calls) == 1:
            raise RuntimeError("original protocol failure")
        if diagnostic_fails:
            raise OSError("diagnostic unavailable")
        return SandboxCommandResult(
            exit_code=0,
            stdout=json.dumps(
                {
                    "ksadk_version": "0.8.4",
                    "pinned_protocol": None,
                    "ksadk_file": "/fixture/fake-secret/ksadk/__init__.py",
                    "untrusted_extra": "must-not-be-logged",
                }
            ),
        )

    session = SimpleNamespace(sandbox_id="fixture", run_command=run, kill=lambda: killed.append(1))
    backend = E2BSkillRuntimeBackend(template_id="fixture")
    backend.sandbox_backend = SimpleNamespace(create_session=lambda **kwargs: session)
    result = backend.run_workflow(
        "test",
        skill_space_ids=[],
        session_id="fixture",
        pinned_packages=[],
        env={"CUSTOM_SECRET": "fake-secret"},
    )
    assert result.error_type == "RuntimeError"
    assert result.error_message == "original protocol failure"
    assert len(calls) == 2
    assert calls[1][0].startswith("python -I -c ")
    assert calls[1][1]["timeout"] == 10
    assert killed == [1]
    assert "fake-secret" not in caplog.text
    assert "must-not-be-logged" not in caplog.text
    if diagnostic_fails:
        assert "step=pinned.version_probe status=failed" in caplog.text
    else:
        assert "step=pinned.version_probe status=report" in caplog.text
        assert '"ksadk_version": "0.8.4"' in caplog.text

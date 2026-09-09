import io
import sys
import time
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from ksadk.resource_runtime import worker
from ksadk.resource_runtime.build_artifacts import write_resource_build
from ksadk.resource_runtime.ipc import encode_frame, read_frame
from ksadk.resource_runtime.worker import WorkerInitialization
from ksadk.sandbox.base import SandboxError
from ksadk.skills.runtime.backends.e2b import E2BSkillRuntimeBackend
from tests.resource_runtime.test_broker import scope
from tests.resource_runtime.test_pinned_skill_execution import (
    SandboxTransportDouble,
    remote_backend,
)
from tests.resource_runtime.test_resource_build_artifacts import frozen as frozen
from tests.resource_runtime.test_skill_execution_target import execution_snapshot


def initialization(tmp_path, frozen):
    snapshot = execution_snapshot(frozen)
    reference = write_resource_build(tmp_path / "build", snapshot, {"skill-binding": [frozen[1]]})
    locked = scope().model_copy(
        update={
            "binding_id": "skill-binding",
            "binding_snapshot_digest": snapshot.digest,
            "allowed_operations": ("execute_skills", "load_skill", "read_skill_artifact"),
        }
    )
    return WorkerInitialization.model_validate(
        {
            "resourceSnapshot": snapshot.model_dump(by_alias=True),
            "scopes": [locked.model_dump(by_alias=True)],
            "bindings": [
                {
                    "config": snapshot.bindings[0].config.model_dump(by_alias=True),
                    "buildDirectory": str(tmp_path / "build"),
                    "buildReference": reference.model_dump(by_alias=True),
                    "cacheDirectory": str(tmp_path / "cache"),
                    "executionConnection": snapshot.bindings[
                        0
                    ].skill_execution.connection.model_dump(by_alias=True),
                    "executionApiKey": "fake-execution-secret",
                    "artifactDirectory": str(tmp_path / "out"),
                }
            ],
        }
    )


def test_secret_only_in_private_pipe_and_connection_drift_rejected(tmp_path, frozen):
    init = initialization(tmp_path, frozen)
    assert "fake-execution-secret" not in init.model_dump_json()
    assert "fake-execution-secret" not in repr(init)
    payload = init.pipe_payload()
    assert payload["bindings"][0]["executionApiKey"] == "fake-execution-secret"
    payload["bindings"][0]["executionConnection"]["endpoint"] = "https://other.example.test"
    with pytest.raises(ValidationError, match="does not match Build"):
        WorkerInitialization.model_validate(payload)


def test_execution_scope_requires_complete_target(tmp_path, frozen):
    init = initialization(tmp_path, frozen)
    payload = init.pipe_payload()
    payload["bindings"][0]["artifactDirectory"] = None
    with pytest.raises(ValidationError):
        WorkerInitialization.model_validate(payload)


def test_runtime_preflight_requires_sdk_without_creating_sandbox(monkeypatch):
    monkeypatch.setitem(sys.modules, "e2b", None)
    with pytest.raises(SandboxError, match="required"):
        E2BSkillRuntimeBackend(template_id="fixture").preflight()

    class Available:
        @staticmethod
        def create(**kwargs):
            raise AssertionError("Preflight must not create a sandbox")

    E2BSkillRuntimeBackend(template_id="fixture", sandbox_cls=Available).preflight()


def test_worker_does_not_report_ready_when_sdk_is_missing(tmp_path, frozen, monkeypatch):
    init = initialization(tmp_path, frozen)
    monkeypatch.setitem(sys.modules, "e2b", None)
    output = io.BytesIO()
    monkeypatch.setattr(
        worker.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(encode_frame(init.pipe_payload())))
    )
    monkeypatch.setattr(worker.sys, "stdout", SimpleNamespace(buffer=output))
    monkeypatch.setattr(worker.logging, "disable", lambda *args: None)
    assert worker.main() == 1
    output.seek(0)
    assert read_frame(output) == {"error": {"code": "RESOURCE_WORKER_INITIALIZATION_FAILED"}}
    assert "fake-execution-secret" not in output.getvalue().decode()


def test_worker_executes_real_pinned_script_with_sandbox_transport_double(
    tmp_path, frozen, monkeypatch
):
    init = initialization(tmp_path, frozen)
    remote = SandboxTransportDouble(tmp_path / "sandbox")
    backend = remote_backend(remote)
    backend.sandbox_backend.check_available = lambda: None

    def factory(snapshot, binding_id, *, current_connection, api_key, artifact_directory):
        assert current_connection == snapshot.bindings[0].skill_execution.connection
        assert api_key.get_secret_value() == "fake-execution-secret"
        backend.artifact_directory = artifact_directory
        return backend

    monkeypatch.setattr(worker, "create_bound_skill_runtime", factory)
    request = {
        "v": 1,
        "requestId": "a" * 64,
        "handle": "h" * 40,
        "operation": "execute_skills",
        "deadline": int((time.time() + 15) * 1000),
        "arguments": {"workflowPrompt": "Generate a report", "skillIds": ["skill-a"]},
    }
    data = encode_frame(init.pipe_payload()) + encode_frame(
        {
            "request": request,
            "scope": init.scopes[0].model_dump(by_alias=True),
        }
    )
    output = io.BytesIO()
    monkeypatch.setattr(worker.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(data)))
    monkeypatch.setattr(worker.sys, "stdout", SimpleNamespace(buffer=output))
    # Do not change pytest's process-wide logging state.
    monkeypatch.setattr(worker.logging, "disable", lambda *args: None)
    assert worker.main() == 0
    output.seek(0)
    assert read_frame(output) == {"status": "ready"}
    result = read_frame(output)["result"]
    assert result["status"] == "succeeded"
    assert result["operationId"] == "a" * 64
    from pathlib import Path

    path = Path(result["outputFiles"][0])
    assert path.is_relative_to((tmp_path / "out").resolve())
    assert path.read_text() == "version-one"
    assert remote.killed
    assert "fake-execution-secret" not in output.getvalue().decode()

import io
import time
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from ksadk.resource_runtime import worker
from ksadk.resource_runtime.discovery_receipts import DiscoverySkillReceipt, discovery_scope_key
from ksadk.resource_runtime.ipc import encode_frame, read_frame
from ksadk.resource_runtime.snapshots import ResourceSnapshot
from ksadk.resource_runtime.worker import WorkerInitialization
from ksadk.skills.package_store import PackageStore
from tests.resource_runtime.test_broker import scope
from tests.resource_runtime.test_pinned_skill_execution import (
    SandboxTransportDouble,
    remote_backend,
)
from tests.resource_runtime.test_resource_build_artifacts import frozen as frozen
from tests.resource_runtime.test_skill_execution_delivery import Executed, configured
from tests.resource_runtime.test_skill_execution_target import execution_snapshot


def initialization(tmp_path, frozen):
    payload = execution_snapshot(frozen).model_dump(by_alias=True)
    payload["bindings"][0]["config"].update(selectionMode="discovery", selectedSkills=[])
    payload["bindings"][0]["connection"]["authMode"] = "signed"
    snapshot = ResourceSnapshot.model_validate(payload)
    binding = snapshot.bindings[0]
    owner = scope().model_copy(update={
        "binding_id": "skill-binding", "binding_snapshot_digest": snapshot.digest,
        "allowed_operations": ("execute_skills", "load_skill", "read_skill_artifact"),
    })
    selection = DiscoverySkillReceipt.from_ref(frozen[1].ref)
    init = WorkerInitialization.model_validate({
        "resourceSnapshot": snapshot.model_dump(by_alias=True),
        "scopes": [owner.model_dump(by_alias=True)],
        "bindings": [{
            "config": binding.config.model_dump(by_alias=True),
            "endpoint": binding.connection.endpoint,
            "accessKey": "fake-skill-access", "secretKey": "fake-skill-secret",
            "cacheDirectory": str(tmp_path / "cache"), "selectionRunRef": "run-a",
            "restoredSelections": [selection.model_dump(by_alias=True)],
            "executionConnection": binding.skill_execution.connection.model_dump(by_alias=True),
            "executionApiKey": "fake-sandbox-secret", "artifactDirectory": str(tmp_path / "out"),
        }],
    })
    PackageStore(
        tmp_path / "cache", namespace=discovery_scope_key(owner, "run-a"), require_hash=True,
    ).store_archive(frozen[1].ref, frozen[1].archive_path.read_bytes())
    return init, selection


@pytest.mark.parametrize("selection_mode", ["matching", "missing", "changed"])
def test_worker_discovery_executes_only_host_committed_package(
    tmp_path, frozen, monkeypatch, selection_mode,
):
    init, selected = initialization(tmp_path, frozen)
    assert "fake-sandbox-secret" not in repr(init)
    assert init.pipe_payload()["bindings"][0]["executionApiKey"] == "fake-sandbox-secret"
    remote = SandboxTransportDouble(tmp_path / "sandbox")
    backend = remote_backend(remote)
    backend.sandbox_backend.check_available = lambda: None
    opened = []

    def create_session(**kwargs):
        opened.append(True)
        return remote

    backend.sandbox_backend.create_session = create_session

    def factory(snapshot, binding_id, *, current_connection, api_key, artifact_directory):
        assert current_connection == snapshot.bindings[0].skill_execution.connection
        assert api_key.get_secret_value() == "fake-sandbox-secret"
        backend.artifact_directory = artifact_directory
        return backend

    monkeypatch.setattr(worker, "create_bound_skill_runtime", factory)
    selections = [selected.model_dump(by_alias=True)]
    if selection_mode == "missing":
        selections = []
    elif selection_mode == "changed":
        selections[0]["versionId"] = "other-version"
    request = {
        "v": 1, "requestId": "a" * 64, "handle": "h" * 40,
        "operation": "execute_skills", "deadline": int((time.time() + 15) * 1000),
        "arguments": {
            "workflowPrompt": "Generate a report", "skillIds": ["skill-a"],
            "_hostSelections": selections,
        },
    }
    framed = encode_frame(init.pipe_payload()) + encode_frame({
        "request": request, "scope": init.scopes[0].model_dump(by_alias=True),
    })
    output = io.BytesIO()
    monkeypatch.setattr(worker.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(framed)))
    monkeypatch.setattr(worker.sys, "stdout", SimpleNamespace(buffer=output))
    monkeypatch.setattr(worker.logging, "disable", lambda *args: None)
    assert worker.main() == 0
    output.seek(0)
    assert read_frame(output) == {"status": "ready"}
    result = read_frame(output)["result"]
    if selection_mode == "matching":
        from pathlib import Path

        assert result["status"] == "succeeded"
        assert Path(result["outputFiles"][0]).read_text() == "version-one"
        assert remote.killed
        assert len(opened) == 1
    else:
        assert result["status"] == "unknown" and result["outputFiles"] == []
        assert not remote.killed and not opened
    assert b"fake-sandbox-secret" not in output.getvalue()


def test_discovery_execution_rejects_target_drift(tmp_path, frozen):
    init, _ = initialization(tmp_path, frozen)
    payload = init.pipe_payload()
    payload["bindings"][0]["executionConnection"]["endpoint"] = "https://other.example.test"
    with pytest.raises(ValidationError, match="does not match Build"):
        WorkerInitialization.model_validate(payload)


async def test_broker_requires_receipts_and_binds_idempotency_to_selected_bytes(tmp_path):
    class SelectedExecution(Executed):
        async def invoke(self, request, scope):
            assert request.arguments["_hostSelections"] == [selected.model_dump(by_alias=True)]
            return await super().invoke(request, scope)

    executed = SelectedExecution(tmp_path / "outputs")
    owner = scope().model_copy(update={
        "allowed_operations": ("execute_skills", "read_skill_artifact"),
    })
    key = discovery_scope_key(owner, "run-a")
    broker, request, owner = configured(
        tmp_path, executed, resource_scope=owner, discovery_key=key,
    )
    rejected = await broker.dispatch(request)
    assert rejected["error"]["code"] == "RESOURCE_SKILL_SELECTION_REQUIRED"
    assert executed.calls == 0
    selected = DiscoverySkillReceipt(
        skill_id="skill-a", version_id="v1", content_hash="sha256:" + "1" * 64,
        name="test", version="1",
    )
    broker._discovery_receipts.record(key, selected)
    forged = await broker.dispatch({**request, "arguments": {
        **request["arguments"], "_hostSelections": [selected.model_dump(by_alias=True)],
    }})
    assert forged["error"]["code"] == "RESOURCE_ARGUMENTS_INVALID" and executed.calls == 0
    unapproved = await broker.dispatch({**request, "arguments": {
        **request["arguments"], "workflowPrompt": "not approved",
    }})
    assert unapproved["error"]["code"] == "RESOURCE_APPROVAL_REQUIRED" and executed.calls == 0
    first = await broker.dispatch(request)
    assert first["result"]["status"] == "succeeded"
    again = await broker.dispatch(request)
    assert again["result"]["artifacts"] == first["result"]["artifacts"]
    assert executed.calls == 1
    new_key = discovery_scope_key(owner, "run-b")
    changed, retry, _ = configured(
        tmp_path, executed, discovery_key=new_key,
        resource_scope=owner.model_copy(update={"activation_id": "new-activation"}),
    )
    changed._discovery_receipts.record(new_key, selected.model_copy(update={"version_id": "v2"}))
    conflict = await changed.dispatch(retry)
    assert conflict["error"]["code"] == "RESOURCE_OPERATION_CONFLICT"
    assert executed.calls == 1

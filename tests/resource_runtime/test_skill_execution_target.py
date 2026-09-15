import pytest
from pydantic import SecretStr, ValidationError

from ksadk.resource_runtime.build_artifacts import restore_resource_build, write_resource_build
from ksadk.resource_runtime.skill_runtime import create_bound_skill_runtime
from ksadk.resource_runtime.skills import PinnedSkillService
from ksadk.resource_runtime.snapshots import ResourceSnapshot
from ksadk.skills.runtime.base import SkillRuntimeResult
from tests.resource_runtime.test_resource_build_artifacts import frozen as frozen


def execution_snapshot(frozen):
    snapshot, _ = frozen
    payload = snapshot.model_dump(by_alias=True)
    binding = payload["bindings"][0]
    binding["config"]["executionMode"] = "isolated"
    binding["skillExecution"] = {
        "connection": {
            "connectionRef": "sandbox-connection",
            "tenantRef": "tenant-a",
            "principalRef": "sandbox-account",
            "endpoint": "https://api.example.test",
            "authMode": "token",
        },
        "domain": "example.test",
        "templateId": "template-v1",
        "timeout": 120,
        "allowInternetAccess": False,
    }
    return ResourceSnapshot.model_validate(payload)


@pytest.mark.parametrize(
    "field,value",
    [
        ("domain", "other.example.test"),
        ("templateId", "template-v2"),
        ("timeout", 60),
        ("allowInternetAccess", True),
    ],
)
def test_execution_target_changes_build_identity(frozen, field, value):
    snapshot = execution_snapshot(frozen)
    payload = snapshot.model_dump(by_alias=True)
    payload["bindings"][0]["skillExecution"][field] = value
    assert ResourceSnapshot.model_validate(payload).digest != snapshot.digest


def test_runtime_uses_frozen_target_and_accepts_secret_rotation(frozen):
    snapshot = execution_snapshot(frozen)
    target = snapshot.bindings[0].skill_execution
    for key in ("fake-first", "fake-rotated"):
        runtime = create_bound_skill_runtime(
            snapshot, "skill-binding", current_connection=target.connection, api_key=SecretStr(key)
        )
        assert runtime.template_id == "template-v1"
        assert runtime.timeout == 120
        assert runtime.allow_internet_access is False
        assert runtime.sandbox_backend.connection.api_key.get_secret_value() == key
        assert key not in snapshot.model_dump_json()


@pytest.mark.parametrize(
    "field,value",
    [
        ("endpoint", "https://other.example.test"),
        ("principal_ref", "other-account"),
    ],
)
def test_connection_drift_rejected(frozen, field, value):
    snapshot = execution_snapshot(frozen)
    current = snapshot.bindings[0].skill_execution.connection.model_copy(update={field: value})
    with pytest.raises(ValueError, match="RESOURCE_CONNECTION_CHANGED"):
        create_bound_skill_runtime(
            snapshot, "skill-binding", current_connection=current, api_key=SecretStr("fake-key")
        )


def test_wrong_tenant_or_execution_mode_rejected(frozen):
    snapshot = execution_snapshot(frozen)
    payload = snapshot.model_dump(by_alias=True)
    payload["bindings"][0]["skillExecution"]["connection"]["tenantRef"] = "other-tenant"
    with pytest.raises(ValidationError):
        ResourceSnapshot.model_validate(payload)
    payload = snapshot.model_dump(by_alias=True)
    payload["bindings"][0]["config"]["executionMode"] = "outer-agent"
    with pytest.raises(ValidationError):
        ResourceSnapshot.model_validate(payload)


def test_execution_target_survives_offline_build_restore(tmp_path, frozen):
    snapshot = execution_snapshot(frozen)
    reference = write_resource_build(tmp_path / "build", snapshot, {"skill-binding": [frozen[1]]})
    restored, _ = restore_resource_build(
        tmp_path / "build", reference, cache_directory=tmp_path / "restore"
    )
    assert restored.snapshot.bindings[0].skill_execution == snapshot.bindings[0].skill_execution
    assert restored.snapshot.digest == snapshot.digest


@pytest.mark.parametrize("requested,expected", [(900, 120), (60, 60)])
def test_execution_budget_cannot_exceed_build_limit(tmp_path, frozen, requested, expected):
    snapshot = execution_snapshot(frozen)
    reference = write_resource_build(tmp_path / "build", snapshot, {"skill-binding": [frozen[1]]})
    service = PinnedSkillService(
        tmp_path / "build",
        reference,
        binding_id="skill-binding",
        cache_directory=tmp_path / "runtime",
    )
    calls = []

    class Backend:
        def run_workflow(self, *args, **kwargs):
            calls.append(kwargs)
            return SkillRuntimeResult(exit_code=0)

    service.execute(
        {"workflowPrompt": "task", "skillIds": ["skill-a"]},
        backend=Backend(),
        operation_id="a" * 64,
        timeout=requested,
    )
    assert calls[0]["timeout"] == expected

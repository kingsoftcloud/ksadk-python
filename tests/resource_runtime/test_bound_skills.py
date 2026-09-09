import asyncio
import json
import os
import shutil
import time
from pathlib import Path

import pytest

from ksadk.resource_runtime.broker import ResourceBroker
from ksadk.resource_runtime.build_artifacts import write_resource_build
from ksadk.resource_runtime.leases import ResourceLeaseRegistry
from ksadk.resource_runtime.process import ResourceWorkerProcess
from ksadk.resource_runtime.skills import PinnedSkillService
from ksadk.resource_runtime.snapshots import ResourceSnapshot
from ksadk.resource_runtime.socket_server import ResourceSocketServer
from ksadk.resource_runtime.worker import WorkerInitialization
from ksadk.skills.package_store import SkillPackageError
from ksadk.skills.runtime import agent
from ksadk.skills.runtime.backends.local import LocalProcessSkillRuntimeBackend
from tests.resource_runtime.test_broker import scope
from tests.resource_runtime.test_resource_build_artifacts import frozen as frozen


def service(tmp_path, frozen):
    snapshot, package = frozen
    directory = tmp_path / "build"
    reference = write_resource_build(directory, snapshot, {"skill-binding": [package]})
    return PinnedSkillService(
        directory, reference, binding_id="skill-binding", cache_directory=tmp_path / "consumer"
    )


def test_offline_directory_instructions_and_reference(tmp_path, frozen):
    bound = service(tmp_path, frozen)
    shutil.rmtree(tmp_path / "source")
    listed = bound.list_skills({})
    assert [item["skillId"] for item in listed["items"]] == ["skill-a"]
    assert listed["items"][0]["versionId"] == "v1"
    assert "Use reference.txt" in bound.load_skill({"skillId": "skill-a"})["content"]
    reply = bound.read_resource({"skillId": "skill-a", "path": "reference.txt"})
    assert reply["content"] == "version-one"
    assert str(tmp_path) not in str(reply)


def test_resource_continuation_returns_complete_text(tmp_path, frozen):
    bound = service(tmp_path, frozen)
    first = bound.read_resource({"skillId": "skill-a", "path": "reference.txt", "maxChars": 4})
    second = bound.read_resource(
        {"skillId": "skill-a", "path": "reference.txt", "offset": first["nextOffset"]}
    )
    assert first["truncated"]
    assert not second["truncated"]
    assert first["content"] + second["content"] == "version-one"


def test_search_uses_pinned_description_and_rejects_space_override(tmp_path, frozen, monkeypatch):
    bound = service(tmp_path, frozen)
    monkeypatch.setenv("SKILL_SPACE_ID", "different-space")
    matches = bound.search_skills({"query": "reports"})
    assert matches["items"][0]["skillId"] == "skill-a"
    assert matches["items"][0]["description"] == "Generates reports from references"
    assert bound.search_skills({"query": "unrelatednomatch"})["items"] == []
    with pytest.raises(ValueError):
        bound.search_skills({"query": "reports", "spaceId": "other"})
    with pytest.raises(ValueError):
        bound.search_skills({"query": " "})


def test_execute_selected_build_package_with_existing_runtime(tmp_path, frozen, monkeypatch):
    snapshot, package = frozen
    payload = snapshot.model_dump(by_alias=True)
    payload["bindings"][0]["config"]["executionMode"] = "isolated"
    bound = service(tmp_path, (ResourceSnapshot.model_validate(payload), package))
    shutil.rmtree(tmp_path / "source")
    monkeypatch.setenv("KSADK_SELECTED_SKILL_NAMES", "unrelated-skill")
    # Real local subprocess exercises the shared transport. It is not an E2B
    # isolation assertion; production admission must choose the promised backend.
    result = bound.execute(
        {"workflowPrompt": "Generate a report", "skillIds": ["skill-a"]},
        backend=LocalProcessSkillRuntimeBackend(Path(agent.__file__)),
        operation_id="a" * 64,
        timeout=10,
    )
    assert result.ok, result.stderr
    assert Path(result.output_files[0]).read_text() == "version-one"


@pytest.mark.parametrize("case", ["mode", "unknown", "duplicate", "space"])
def test_execution_rejects_invalid_selection_before_backend(tmp_path, frozen, case):
    snapshot, package = frozen
    payload = snapshot.model_dump(by_alias=True)
    if case != "mode":
        payload["bindings"][0]["config"]["executionMode"] = "isolated"
    bound = service(tmp_path, (ResourceSnapshot.model_validate(payload), package))
    arguments = {"workflowPrompt": "Generate", "skillIds": ["skill-a"]}
    if case == "unknown":
        arguments["skillIds"] = ["not-bound"]
    elif case == "duplicate":
        arguments["skillIds"] = ["skill-a", "skill-a"]
    elif case == "space":
        arguments["spaceId"] = "other-space"

    class NoExecution:
        def run_workflow(self, *args, **kwargs):
            pytest.fail("Rejected execution reached Runtime")

    with pytest.raises(ValueError):
        bound.execute(arguments, backend=NoExecution(), operation_id="a" * 64, timeout=10)


@pytest.mark.parametrize("path", ["../secret", "/etc/passwd", "a/../b", "a\\b", "a//b"])
def test_path_escape_rejected(tmp_path, frozen, path):
    bound = service(tmp_path, frozen)
    with pytest.raises(ValueError):
        bound.read_resource({"skillId": "skill-a", "path": path})


def test_unbound_skill_and_space_override_rejected(tmp_path, frozen):
    bound = service(tmp_path, frozen)
    with pytest.raises(ValueError, match="SKILL_NOT_BOUND"):
        bound.load_skill({"skillId": "not-selected"})
    with pytest.raises(ValueError):
        bound.list_skills({"spaceId": "other-space"})


def test_build_archive_changes_do_not_change_runtime_instructions(tmp_path, frozen):
    bound = service(tmp_path, frozen)
    archive = next((tmp_path / "build").glob("*.zip"))
    archive.write_bytes(b"modified bytes")
    with pytest.raises(SkillPackageError):
        bound.load_skill({"skillId": "skill-a"})


def test_modified_extraction_is_not_served(tmp_path, frozen):
    bound = service(tmp_path, frozen)
    extracted = next((tmp_path / "consumer").rglob("reference.txt"))
    extracted.write_text("untrusted replacement")
    assert (
        bound.read_resource({"skillId": "skill-a", "path": "reference.txt"})["content"]
        == "version-one"
    )


@pytest.mark.parametrize("core", [False, True])
async def test_pinned_skill_broker_to_real_worker_without_credentials(tmp_path, frozen, core):
    modules = os.environ.get("KSADK_TEST_DSH_NODE_MODULES")
    if core and not modules:
        pytest.skip("Pinned official DSH modules required")
    bound = service(tmp_path, frozen)
    snapshot, _ = frozen
    locked_scope = scope().model_copy(
        update={
            "binding_id": "skill-binding",
            "binding_snapshot_digest": snapshot.digest,
            "allowed_operations": (
                "list_skills",
                "search_skills",
                "load_skill",
                "read_skill_resource",
            ),
        }
    )
    init = WorkerInitialization.model_validate(
        {
            "resourceSnapshot": snapshot.model_dump(by_alias=True),
            "scopes": [locked_scope.model_dump(by_alias=True)],
            "bindings": [
                {
                    "config": snapshot.bindings[0].config.model_dump(by_alias=True),
                    "buildDirectory": str(bound.directory),
                    "buildReference": bound.reference.model_dump(by_alias=True),
                    "cacheDirectory": str(tmp_path / "worker-cache"),
                }
            ],
        }
    )
    assert "accessKey" not in init.pipe_payload()["bindings"][0]
    shutil.rmtree(tmp_path / "source")
    registry = ResourceLeaseRegistry()
    lease = registry.issue(locked_scope)
    broker = ResourceBroker(registry, locked_scope.generation_id)
    worker = await ResourceWorkerProcess.start(init)
    broker.register(locked_scope, worker)
    server = ResourceSocketServer(broker)
    node = None
    try:
        results = []
        operations = [
            ("list_skills", {}),
            ("load_skill", {"skillId": "skill-a"}),
            ("read_skill_resource", {"skillId": "skill-a", "path": "reference.txt"}),
            ("search_skills", {"query": "build-skill"}),
        ]
        if core:
            path = await server.start()
            root = Path(__file__).resolve().parents[2]
            bundles = root / "ksadk/plugins/providers/bundles"
            node = await asyncio.create_subprocess_exec(
                "node",
                str(root / "tests/fixtures/resource_runtime/node_core_probe.mjs"),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            output, errors = await asyncio.wait_for(
                node.communicate(
                    json.dumps(
                        {
                            "nodeModules": modules,
                            "socketPath": str(path),
                            "bridgePlugin": (bundles / "dsh-platform-resources/index.mjs").as_uri(),
                            "businessPlugin": (bundles / "dsh-skill-center/index.mjs").as_uri(),
                            "toolName": "list_skills",
                            "missingContextArguments": {},
                            "calls": [
                                {"handle": lease.handle, "name": name, "arguments": arguments}
                                for name, arguments in operations
                            ],
                        }
                    ).encode()
                ),
                30,
            )
            assert node.returncode == 0, errors.decode()
            data = json.loads(output)
            assert data["missingContext"]["isError"] is True
            assert all(not result["isError"] for result in data["results"]), data
            results = [result["value"] for result in data["results"]]
        for operation, arguments in [] if core else operations:
            reply = await broker.dispatch(
                {
                    "v": 1,
                    "requestId": operation,
                    "operation": operation,
                    "handle": lease.handle,
                    "deadline": int((time.time() + 10) * 1000),
                    "arguments": arguments,
                }
            )
            assert reply["result"]["status"] == "ok", reply
            results.append(reply["result"])
        assert results[0]["items"][0]["skillId"] == "skill-a"
        assert "Use reference.txt" in results[1]["content"]
        assert results[2]["content"] == "version-one"
        assert results[3]["items"][0]["skillId"] == "skill-a"
        assert results[3]["items"][0]["reason"] == "name"
        invalid = {
            "v": 1,
            "requestId": "invalid-path",
            "operation": "read_skill_resource",
            "handle": lease.handle,
            "deadline": int((time.time() + 10) * 1000),
            "arguments": {"skillId": "skill-a", "path": "../private"},
        }
        rejected = await broker.dispatch(invalid)
        assert rejected["result"] == {"status": "failed", "errorCode": "SKILL_RESOURCE_UNAVAILABLE"}
        valid = await broker.dispatch(
            {
                **invalid,
                "requestId": "after-invalid",
                "arguments": {
                    "skillId": "skill-a",
                    "path": "reference.txt",
                },
            }
        )
        assert valid["result"]["content"] == "version-one"
    finally:
        if node is not None and node.returncode is None:
            node.kill()
            await node.wait()
        await server.aclose()
        await worker.aclose()

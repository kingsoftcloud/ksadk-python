import json
import time
from dataclasses import asdict
from unittest.mock import AsyncMock

import pytest

from ksadk.plugins.execution_host import ExecutionReceipt, PluginExecutionScope
from ksadk.plugins.teams.errors import TeamsError
from ksadk.studio.teams_node import TeamsExecutionNode


def node(tmp_path):
    client = AsyncMock()
    client.request.return_value = {"accepted": True}
    host = AsyncMock()
    host.source_events.return_value = {"items": [], "cursor": 0}
    host.interactions.return_value = []
    value = TeamsExecutionNode(
        client, host, state_dir=tmp_path, name="test", bindings=[{"bindingRef": "local-build:one"}]
    )
    value.identity.update(
        nodeId="node-one",
        tenantId="tenant",
        ownerSubject="owner",
        authorityRef="server",
        pluginDigest="sha256:fixture",
    )
    value._deadline = time.monotonic() + 45
    scope = PluginExecutionScope(
        "io.ksadk.teams",
        "sha256:fixture",
        "server",
        "tenant",
        "owner",
        "node:node-one:local-build:one",
        "session-one",
    )
    command = {
        "commandId": "command-one",
        "nodeId": "node-one",
        "epoch": 1,
        "operation": "submit",
        "scope": asdict(scope),
        "arguments": {
            "content": "work",
            "idempotency_key": "delivery-one",
            "grant_id": "grant-one",
            "causation": "decision-one",
            "policy_context": {"deliveryId": "delivery-one"},
        },
    }
    value.db.execute(
        "INSERT INTO commands(id,body) VALUES(?,?)", (command["commandId"], json.dumps(command))
    )
    return value, command, host, client


@pytest.mark.asyncio
async def test_lost_result_ack_replays_exact_report_without_resubmitting(tmp_path):
    value, command, host, client = node(tmp_path)
    host.lookup.return_value = ExecutionReceipt(
        "accepted", run_id="run-one", run_status="succeeded"
    )
    client.request.side_effect = [TeamsError("offline", "offline", status=503), {"accepted": True}]
    try:
        with pytest.raises(TeamsError):
            await value._execute(command)
        await value._execute(command)
        assert client.request.call_args_list[0].args[2] == client.request.call_args_list[1].args[2]
        host.submit.assert_not_called()
        assert value.db.execute("SELECT complete FROM commands").fetchone()[0] == 1
    finally:
        await value.close()


@pytest.mark.asyncio
async def test_crash_after_admission_queries_original_key(tmp_path):
    value, command, host, client = node(tmp_path)
    host.lookup.return_value = ExecutionReceipt("accepted", run_id="run-one", run_status="running")
    try:
        await value._execute(command)
        host.lookup.assert_awaited_once()
        assert host.lookup.call_args.args[1] == "delivery-one"
        assert host.lookup.call_args.args[0].binding_ref == "local-build:one"
        host.submit.assert_not_called()
        assert value.db.execute("SELECT complete FROM commands").fetchone()[0] == 0
    finally:
        await value.close()


@pytest.mark.asyncio
async def test_expired_lease_does_not_admit_a_new_execution(tmp_path):
    value, command, host, client = node(tmp_path)
    value._deadline = 0
    host.lookup.return_value = ExecutionReceipt("missing")
    try:
        await value._execute(command)
        host.submit.assert_not_called()
        client.request.assert_not_called()
    finally:
        await value.close()


@pytest.mark.asyncio
async def test_lookup_failure_remains_uncertain_instead_of_false_rejection(tmp_path):
    value, command, host, client = node(tmp_path)
    host.lookup.side_effect = OSError("sensitive upstream details")
    try:
        await value._execute(command)
        report = client.request.call_args.args[2]
        assert report["result"]["status"] == "uncertain"
        assert "sensitive" not in json.dumps(report)
        assert value.db.execute("SELECT complete FROM commands").fetchone()[0] == 0
    finally:
        await value.close()


@pytest.mark.asyncio
async def test_preparation_failure_is_not_reported_as_unknown_submission(tmp_path):
    value, command, host, client = node(tmp_path)
    host.lookup.return_value = ExecutionReceipt("missing")
    host.ensure_session.side_effect = TeamsError("BUILD_UNAVAILABLE", "missing build", status=422)
    try:
        await value._execute(command)
        host.submit.assert_not_called()
        report = client.request.call_args.args[2]
        assert report["result"] == {"status": "rejected", "reason": "BUILD_UNAVAILABLE"}
        assert value.db.execute("SELECT complete FROM commands").fetchone()[0] == 1
    finally:
        await value.close()


@pytest.mark.asyncio
async def test_same_workspace_node_cannot_run_twice_and_can_restart_after_close(tmp_path):
    value, _, host, client = node(tmp_path)
    try:
        with pytest.raises(TeamsError, match="已在运行"):
            TeamsExecutionNode(client, host, state_dir=tmp_path, name="duplicate", bindings=[])
    finally:
        await value.close()
    restored = TeamsExecutionNode(client, host, state_dir=tmp_path, name="restored", bindings=[])
    await restored.close()


@pytest.mark.asyncio
async def test_failed_journal_initialization_releases_node_lock(tmp_path):
    (tmp_path / "identity.json").write_text("broken json")
    with pytest.raises(json.JSONDecodeError) as error:
        TeamsExecutionNode(AsyncMock(), AsyncMock(), state_dir=tmp_path, name="broken", bindings=[])
    (tmp_path / "identity.json").write_text('{"nodeId":"restored"}')
    restored = TeamsExecutionNode(
        AsyncMock(), AsyncMock(), state_dir=tmp_path, name="fixed", bindings=[]
    )
    assert error.value is not None  # Keep the failed constructor's traceback alive.
    await restored.close()


@pytest.mark.asyncio
async def test_wrong_tenant_or_binding_cannot_be_executed(tmp_path):
    value, command, host, client = node(tmp_path)
    command["scope"]["tenant_id"] = "someone-else"
    try:
        with pytest.raises(TeamsError, match="不属于"):
            await value._execute(command)
        host.submit.assert_not_called()
    finally:
        await value.close()


@pytest.mark.asyncio
async def test_superseded_report_is_quarantined_without_blocking_other_commands(tmp_path):
    value, command, host, client = node(tmp_path)
    host.lookup.return_value = ExecutionReceipt("accepted", run_id="run-one", run_status="running")
    client.request.side_effect = TeamsError("execution_epoch_expired", "expired", status=409)
    try:
        await value._execute_checked(command)
        row = value.db.execute("SELECT complete,result FROM commands").fetchone()
        assert row[0] == 2
        assert json.loads(row[1])["result"]["run_status"] == "running"
        host.submit.assert_not_called()
    finally:
        await value.close()


@pytest.mark.asyncio
async def test_execution_node_runner_closes_services_on_stop(tmp_path):
    import asyncio
    from types import SimpleNamespace

    from ksadk.studio.teams_node_runner import run_node

    stopped = asyncio.Event()
    stopped.set()
    service = SimpleNamespace(
        teams_installation=SimpleNamespace(enable=AsyncMock(), node=object()), aclose=AsyncMock()
    )
    created = []

    def factory(root, **kwargs):
        created.append((root, kwargs))
        return service

    await run_node(
        tmp_path, "http://localhost:8000/teams", kind="cloud", stop=stopped, service_factory=factory
    )
    assert created[0][1]["configuration_overrides"]["KSADK_TEAMS_NODE_KIND"] == "cloud"
    service.teams_installation.enable.assert_awaited_once()
    service.aclose.assert_awaited_once()


@pytest.mark.asyncio
async def test_fenced_node_observes_late_terminal_without_resubmitting(tmp_path):
    value, command, host, client = node(tmp_path)
    host.lookup.return_value = ExecutionReceipt(
        "accepted", run_id="run-one", run_status="succeeded"
    )
    client.request.side_effect = TeamsError("execution_epoch_expired", "expired", status=409)
    value.db.execute("UPDATE commands SET complete=2")
    try:
        await value._observe_fenced(command)
        assert value.db.execute("SELECT complete FROM commands").fetchone()[0] == 3
        assert client.request.call_args.args[2]["result"]["run_status"] == "succeeded"
        host.submit.assert_not_called()
        host.cancel.assert_not_called()
    finally:
        await value.close()

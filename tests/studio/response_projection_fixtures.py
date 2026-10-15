"""Read-only Responses replay of a freshly reopened Studio event store."""

from types import SimpleNamespace

from ksadk.runtime import RuntimeExecutor, RuntimeRegistry
from ksadk.studio.contracts import RunRecord
from ksadk.studio.run_service import StudioRunService
from ksadk.studio.shared_web import StudioSharedWebBridge
from ksadk.studio.workspace import Workspace


async def replay_responses(tmp_path, entries):
    workspace = Workspace(tmp_path)
    workspace.initialize()
    service = StudioRunService(workspace, RuntimeExecutor(RuntimeRegistry()))
    service.event_store.create(
        RunRecord(
            id="run_fixture",
            build_id="build",
            agent_id="agent",
            session_id="session",
            trace_id="trace",
            runtime_type="harness",
            input="fixture",
        )
    )
    service.event_store.append_many("run_fixture", entries)
    # Use real persistence and cold readers without booting unrelated services.
    restored = StudioRunService(Workspace(tmp_path), RuntimeExecutor(RuntimeRegistry()))
    bridge = StudioSharedWebBridge(
        SimpleNamespace(
            event_store=restored.event_store,
            run_service=restored,
        )
    )
    bridge._run_ids_by_invocation["invocation"] = "run_fixture"
    seen = set()
    responses = await bridge._project_response_events(
        session_id="session",
        agent_id="agent",
        invocation_id="invocation",
        seen=seen,
    )
    assert (
        await bridge._project_response_events(
            session_id="session",
            agent_id="agent",
            invocation_id="invocation",
            seen=seen,
        )
        == []
    )
    return responses

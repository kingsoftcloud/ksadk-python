import asyncio
from types import SimpleNamespace

import pytest
from google.adk.agents import LlmAgent
from google.adk.integrations.langchain import LangchainTool
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_response import LlmResponse
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from ksadk.a2a.invocation_service import A2AInvocationService
from ksadk.kernel.state import RunState
from ksadk.kernel.worker import AgentKernelWorker
from ksadk.runtime.framework_adapters import ADKRuntimeAdapter
from ksadk.toolsets.a2a import get_a2a_tools
from tests.a2a.test_invocation_service_r85 import Wire, request
from tests.kernel.control_harness import AGENT, TENANT, command, kernel_stack
from tests.runners.test_adk_multi_llm_runtime_events import runner_for


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "action,fixed",
    [(action, False) for action in ["approve", "unknown", "reject", "cancel", "conflict", "unbound", "foreign", "list", "card"]]
    + [(action, True) for action in ["approve", "unknown", "reject", "cancel", "conflict", "unbound", "foreign"]]
    + [(action, "composed") for action in ["approve", "unknown", "reject", "cancel"]]
    + [("timeout", True)],
)
async def test_real_adk_function_tool_uses_kernel_approval_and_original_call(monkeypatch, action, fixed):
    spec = request().spec
    if action == "timeout":
        spec = spec.model_copy(update={"timeout_seconds": 1.0})
    monkeypatch.setenv("KSADK_A2A_SPACE_ID", spec.space_id)
    replies = []
    if action in {"list", "card"}:

        def legacy_discovery():
            raise AssertionError("managed discovery must not use legacy KOP")

        monkeypatch.setattr("ksadk.toolsets.a2a._discover_agents", legacy_discovery)
    if action in {"unbound", "foreign"}:
        from ksadk.events.run_authority import RunEventAuthority

        original_list = RunEventAuthority.list

        async def unrelated_facts(authority, *args, **kwargs):
            facts = await original_list(authority, *args, **kwargs)
            return [
                event.model_copy(update={"scope_id": "another-native-invocation"})
                if getattr(event, "item_kind", None) == "tool_call"
                else event
                for event in facts
                if action != "unbound" or getattr(event, "item_kind", None) != "tool_call"
            ]

        monkeypatch.setattr(RunEventAuthority, "list", unrelated_facts)

    class FixtureLlm(BaseLlm):
        model: str = "fixture"

        async def generate_content_async(self, llm_request, stream=False):
            responses = [
                p.function_response
                for c in llm_request.contents
                for p in c.parts
                if p.function_response is not None
            ]
            if responses:
                replies.extend(responses)
                yield LlmResponse(
                    content=types.Content(role="model", parts=[types.Part(text="done")])
                )
            else:
                yield LlmResponse(
                    content=types.Content(
                        role="model",
                        parts=[
                            types.Part(
                                function_call=types.FunctionCall(
                                    id="call_1",
                                    name=(
                                        "list_a2a_agents"
                                        if action == "list"
                                        else "get_a2a_agent_card"
                                        if action == "card"
                                        else spec.local_name if fixed else "call_a2a_agent"
                                    ),
                                    args=(
                                        {}
                                        if action == "list"
                                        else {"agent": spec.target_agent_id}
                                        if action == "card"
                                        else {"message": "hello"} if fixed else {"agent": spec.target_agent_id, "message": "hello"}
                                    ),
                                ),
                            )
                        ],
                    )
                )

    from ksadk.a2a.adk import handoff_tool
    root = LlmAgent(
        name="root", model=FixtureLlm(), tools=([handoff_tool(spec)] if fixed is True else [] if fixed == "composed" else [LangchainTool(tool) for tool in get_a2a_tools()])
    )
    if fixed == "composed":
        from ksadk.studio.a2a_orchestration import compile_orchestration
        from ksadk.studio.contracts import RemoteAgentBinding
        from tests.studio.test_a2a_orchestration import draft
        definition = draft(handoff=True, framework="adk")
        binding = RemoteAgentBinding.freeze(
            local_name=spec.local_name, space_id=spec.space_id, target_agent_id=spec.target_agent_id,
            expected_version_id=spec.expected_version_id, exposure=spec.exposure, policy_ref="policy://a2a/runtime")
        definition.spec.bindings.remote_agents = [binding]
        definition.spec.a2a_orchestration.nodes[1].binding_id = binding.binding_id
        namespace = {}
        exec(compile_orchestration(definition).source, namespace)
        original = root
        root = namespace["compose"]({"local": original})
        assert original.tools == [] and original.parent_agent is None
    if action == "conflict":
        from ksadk.a2a.adk import remote_agent

        root.sub_agents = [remote_agent(spec)]
    native_sessions = InMemorySessionService()
    await native_sessions.create_session(app_name="fixture", user_id="user", session_id="s1")
    native = Runner(agent=root, app_name="fixture", session_service=native_sessions)
    runner = runner_for([])
    runner._agent = root

    async def prepare(**kwargs):
        return native.run_async(
            user_id="user",
            session_id="s1",
            new_message=types.Content(
                role="user",
                parts=[types.Part(text="call expert")],
            ),
            run_config=kwargs["run_config"],
        )

    runner._prepare_run_events = prepare
    wire = Wire(events=[] if action == "unknown" else None)
    if action == "timeout":
        from a2a.types import Message, Part, Role, Task, TaskState, TaskStatus

        wire.task = Task(id="remote-1", context_id="context-1", status=TaskStatus(
            state=TaskState.TASK_STATE_INPUT_REQUIRED,
            message=Message(message_id="question", role=Role.ROLE_AGENT,
                            parts=[Part(text="Please provide input")]),
        ))
        wire.events = [wire.task]

        async def record_cancel(prepared):
            wire.calls.append(("remote_cancel", prepared))
            return wire.task

        wire.cancel_prepared_task = record_cancel

    async def discover():
        assert not fixed, "fixed targets must not rediscover or select a new version"
        return [
            SimpleNamespace(
                agent_id=spec.target_agent_id,
                version_id=spec.expected_version_id,
                agent_card=SimpleNamespace(
                    name="expert",
                    url="http://127.0.0.1/private",
                    security_schemes={"token": "fake-private-value"},
                ),
                endpoint="http://127.0.0.1/private",
                credential="fake-private-value",
            )
        ]

    adapter = ADKRuntimeAdapter(runner)
    adapter.configure_managed_a2a(
        SimpleNamespace(
            outbound_enabled=True,
            runtime_metadata=SimpleNamespace(agent_id="caller"),
            client_for_space=lambda _: SimpleNamespace(discover=discover),
            invocation_service_for_space=lambda _, **deps: A2AInvocationService(
                space_client=wire, **deps
            ),
        )
    )
    stack = await kernel_stack(adapter=adapter)
    lease = await stack.lease()
    worker = AgentKernelWorker(
        stack.store, adapter_factory=lambda: adapter, session_events=stack.events
    )
    await stack.kernel.submit(
        command(
            idempotency_key="tool-run",
            payload={
                "content": "call expert",
                "session_context": {},
            },
        ),
        permit=stack.permit("enqueue"),
    )
    started = await worker.run_once(AGENT, lease)
    if action in {"list", "card"}:

        async def discovery_finished():
            while True:
                run = await stack.store.load_run(started.run_id)
                if run.state in {RunState.COMPLETED, RunState.FAILED, RunState.INTERRUPTED}:
                    return run
                await asyncio.sleep(0.01)

        finished = await asyncio.wait_for(discovery_finished(), 5)
        assert finished.state == RunState.COMPLETED and len(replies) == 1
        result = replies[0].response
        assert result["ok"] is True, result
        entry = result["agents"][0] if action == "list" else result["agent"]
        assert entry["agent_id"] == spec.target_agent_id
        assert entry["version_id"] == spec.expected_version_id
        assert "url" not in entry and "endpoint" not in entry
        assert "127.0.0.1" not in str(result) and "fake-private-value" not in str(result)
        assert not wire.calls
        assert not await stack.store.list_pending_interactions(TENANT, "s1")
        return
    if action in {"conflict", "unbound", "foreign"}:

        async def rejected():
            while not replies:
                await asyncio.sleep(0.01)

        await asyncio.wait_for(rejected(), 5)
        assert replies[0].response["error_type"] == (
            "A2A_EXPOSURE_CONFLICT" if action == "conflict" else "A2A_NATIVE_TRIGGER_NOT_PERSISTED"
        )
        assert not wire.calls
        assert not await stack.store.list_pending_interactions(TENANT, "s1")
        return

    async def pending():
        while True:
            records = await stack.store.list_pending_interactions(TENANT, "s1")
            if records:
                return records[0]
            run = await stack.store.load_run(started.run_id)
            assert run.state not in {RunState.COMPLETED, RunState.FAILED}, replies
            await asyncio.sleep(0.01)

    record = await asyncio.wait_for(pending(), 5)
    assert not wire.calls
    if action == "cancel":
        receipt = await stack.kernel.submit(
            command("interrupt", idempotency_key="cancel-tool"), permit=stack.permit("interrupt")
        )
        assert receipt.status == "accepted"
        result = await asyncio.wait_for(worker.run_once(AGENT, lease), 5)
        assert result.outcome == "completed"
        assert (await stack.store.load_run(started.run_id)).state == RunState.CANCELLED
        assert not wire.calls and not replies
        assert not await stack.store.list_pending_interactions(TENANT, "s1")
        return
    receipt = await stack.kernel.submit(
        command(
            "submit_interaction",
            idempotency_key="reply",
            payload={
                "run_id": started.run_id,
                "interaction_id": record.interaction_id,
                "token_ref": "fake-ref",
                "action": "approve" if action in {"unknown", "timeout"} else action,
                "response": {},
                "expected_revision": record.revision,
            },
        ),
        permit=stack.permit("submit_interaction"),
    )
    assert receipt.status == "accepted"
    assert (await worker.run_once(AGENT, lease)).outcome == "completed"

    async def completed():
        while True:
            run = await stack.store.load_run(started.run_id)
            if run.state in {RunState.COMPLETED, RunState.FAILED}:
                return run
            await asyncio.sleep(0.01)

    run = await asyncio.wait_for(completed(), 5)
    if action == "timeout":
        errors = [row.payload.get("error") for row in await stack.events.read("s1", 0, 200)
                  if row.event_type == "run.failed"]
        # This wire still reports input_required after cancel. Never turn an
        # unconfirmed remote cancellation into a successful parent outcome.
        assert run.state == RunState.FAILED
        assert [error["code"] for error in errors] == ["A2A_DELEGATE_OUTCOME_UNCONFIRMED"]
        assert len(replies) == 1 and replies[0].response["error_type"] == "A2A_OVERALL_TIMEOUT"
        assert sum(kind == "send" for kind, _ in wire.calls) == 1
        assert sum(kind == "remote_cancel" for kind, _ in wire.calls) == 1
        return
    assert run.state == (RunState.FAILED if action == "unknown" else RunState.COMPLETED)
    assert len(replies) == 1 and replies[0].id == "call_1"
    result = replies[0].response
    assert result["ok"] is (action == "approve")
    assert sum(kind == "send" for kind, _ in wire.calls) == (
        1 if action in {"approve", "unknown"} else 0
    )
    if action == "approve":
        assert result["reply"] == "hello"
        from ksadk.events.canonical_store import RuntimeEventStore
        from ksadk.events.content import DataContent, ToolCallContent

        stored = await RuntimeEventStore(stack.events, session_id="s1").list(
            "s1", run_id=started.run_id
        )
        calls, descriptors = [], []
        for event in stored:
            snapshot = getattr(event, "snapshot", None)
            if snapshot is None:
                continue
            for part in snapshot.parts:
                if isinstance(part, ToolCallContent) and part.call_id == "call_1":
                    calls.append((event.scope_id, event.item_id))
                if isinstance(part, DataContent) and isinstance(part.data, dict):
                    if part.data.get("schema") == "execution.scope/v1":
                        descriptors.append(part.data)
        assert descriptors and calls
        assert all(
            (d["trigger_ref"]["scope_id"], d["trigger_ref"]["item_id"]) in calls
            for d in descriptors
        )
    elif action == "unknown":
        assert result["state"] == "unknown"
        assert result["reply"] == ""
    else:
        assert result["error_type"] == "A2A_APPROVAL_DENIED"


def test_install_only_replaces_sdk_function_and_preserves_call_schema():
    from google.adk.tools.function_tool import FunctionTool

    from ksadk.a2a.adk_tools import ManagedA2ACallTool, install_managed_tools
    from ksadk.toolsets.a2a import call_a2a_agent

    async def unrelated(agent: str, message: str):
        return {"ok": True}

    unrelated.__name__ = "call_a2a_agent"
    custom = FunctionTool(unrelated)
    sdk = LangchainTool(get_a2a_tools()[-1])
    root = LlmAgent(name="root", model="fixture", tools=[sdk, custom])
    install_managed_tools(root)
    assert isinstance(root.tools[0], ManagedA2ACallTool)
    assert root.tools[1] is custom
    assert root.tools[0]._get_declaration() == FunctionTool(call_a2a_agent)._get_declaration()


@pytest.mark.asyncio
async def test_managed_discovery_failure_is_explicit_and_does_not_expose_backend_error():
    from ksadk.a2a.adk_tools import ManagedA2ADiscoveryTool, bind_managed_adk_tools
    from ksadk.toolsets.a2a import get_a2a_agent_card, list_a2a_agents

    async def fail(_):
        raise OSError("fake-private-value")

    runtime = SimpleNamespace(discover=fail)
    with bind_managed_adk_tools(runtime):
        result = await ManagedA2ADiscoveryTool(list_a2a_agents).run_async(
            args={}, tool_context=None
        )
        assert result["ok"] is False and result["error_type"] == "A2A_DISCOVERY_FAILED"
        assert "fake-private-value" not in str(result)
        missing = await ManagedA2ADiscoveryTool(get_a2a_agent_card).run_async(
            args={}, tool_context=None
        )
        assert missing["error_type"] == "A2A_AGENT_REQUIRED"
    unbound = await ManagedA2ADiscoveryTool(list_a2a_agents).run_async(args={}, tool_context=None)
    assert unbound["error_type"] == "A2A_MANAGED_INVOCATION_CONTEXT_REQUIRED"


@pytest.mark.asyncio
async def test_fixed_handoff_schema_contains_message_only_and_fails_without_runtime():
    from ksadk.a2a.adk import handoff_tool
    from google.adk.tools.function_tool import FunctionTool
    spec = request().spec
    tool = handoff_tool(spec)
    async def expert(message: str):
        """Delegate to this fixed remote expert and receive its result."""
        return message
    assert tool._get_declaration() == FunctionTool(expert)._get_declaration()
    result = await tool.run_async(args={"message": "hello"}, tool_context=None)
    assert result == {"ok": False, "error_type": "A2A_MANAGED_INVOCATION_CONTEXT_REQUIRED"}
    with pytest.raises(ValueError, match="A2A_VERSION_PIN_REQUIRED"):
        handoff_tool(spec.model_copy(update={"expected_version_id": None}))

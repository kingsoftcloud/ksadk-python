"""Subprocess fixture: real ADK resume, explicit test policy and fake remote wire."""

import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace

from a2a.types import Message, Part, Role, Task, TaskState, TaskStatus
from google.adk.agents import LlmAgent, SequentialAgent
from google.adk.apps import App, ResumabilityConfig
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_response import LlmResponse
from google.adk.runners import Runner
from google.adk.sessions import DatabaseSessionService
from google.genai import types
from sqlalchemy.ext.asyncio import create_async_engine

from ksadk.a2a.adk import bind_managed_a2a, canonical_event_from_adk, remote_agent
from ksadk.a2a.adk_invocation_context import ADKInvocationContextProvider
from ksadk.a2a.invocation_checkpoint import PostgresA2AInvocationCheckpointStore
from ksadk.a2a.invocation_policy import A2AInvocationPolicy, A2AInvocationPolicyDecision
from ksadk.a2a.invocation_service import A2AInvocationService
from ksadk.a2a.invocation_types import A2AInvocationRequest
from ksadk.events.adapters.adk import ADKAdapterContext, ADKEventAdapter
from ksadk.events.run_authority import RunEventAuthority, bind_run_event_authority
from ksadk.events.session_event import SessionServiceEventStore
from ksadk.interaction.contracts import InteractionReceipt
from ksadk.kernel.contracts import ActivationWriteGuard
from ksadk.sessions.local_service import LocalSessionService
from tests.a2a.test_invocation_service_r85 import Wire, request


class ToolFixtureLlm(BaseLlm):
    model: str = "fixture"

    async def generate_content_async(self, llm_request, stream=False):
        if any(
            part.function_response for content in llm_request.contents for part in content.parts
        ):
            yield LlmResponse(content=types.Content(role="model", parts=[types.Part(text="done")]))
        else:
            yield LlmResponse(
                content=types.Content(
                    role="model",
                    parts=[
                        types.Part(
                            function_call=types.FunctionCall(
                                id="call_1",
                                name="call_a2a_agent",
                                args={"agent": request().spec.target_agent_id, "message": "hello"},
                            )
                        )
                    ],
                )
            )


async def main():
    directory = Path(os.environ["R85_NATIVE_RECOVERY_DIRECTORY"])
    stage = os.environ["R85_NATIVE_RECOVERY_STAGE"]
    pause = os.environ["R85_NATIVE_RECOVERY_PAUSE"]
    workflow = os.environ["R85_NATIVE_RECOVERY_WORKFLOW"]
    native_session_id = "native"
    state_path = directory / "native-state.json"
    sessions = LocalSessionService(directory / "canonical.sqlite")
    if stage == "start":
        await sessions.create_session("caller", "user", "s")
    authority = RunEventAuthority(
        SessionServiceEventStore(sessions),
        session_id="s",
        run_id="public-run",
        guard=ActivationWriteGuard(activation_id=stage, fencing_token=1 if stage == "start" else 2),
        # The service below uses an explicit fixture policy; this unused ledger
        # only permits the production binding to compose its policy closure.
        interaction_ledger=object(),
        tenant_id="tenant",
        agent_instance_id="caller",
    )
    engine = create_async_engine(os.environ["KSADK_A2A_TEST_DATABASE_URL"])
    checkpoints = PostgresA2AInvocationCheckpointStore(
        engine,
        account_id=os.environ["R85_NATIVE_RECOVERY_ACCOUNT"],
        tenant_id="tenant",
        runtime_id="runtime",
    )
    await checkpoints.initialize()
    wire = Wire()
    completed = wire.task
    if pause == "input":
        wire.task = Task(
            id="remote-1",
            context_id="context-1",
            status=TaskStatus(
                state=TaskState.TASK_STATE_INPUT_REQUIRED,
                message=Message(
                    message_id="question", role=Role.ROLE_AGENT, parts=[Part(text="Provide input")]
                ),
            ),
        )
        wire.events = [wire.task]
    service = A2AInvocationService(
        space_client=wire,
        event_store=authority,
        checkpoint_store=checkpoints,
        policy=A2AInvocationPolicy(
            evaluator=lambda *_: A2AInvocationPolicyDecision(
                "approval_required" if stage == "start" and pause == "approval" else "allow"
            )
        ),
    )
    context_provider = ADKInvocationContextProvider(
        authority=authority,
        service_for_space=lambda _: service,
        caller_agent_id="caller",
        message_selector=lambda *_: ("hello",),
    )
    initial = None

    async def provider(ctx, spec):
        nonlocal initial
        resolved = await context_provider(ctx, spec)
        initial = resolved.request
        return resolved

    agent = remote_agent(request().spec)
    if workflow == "sequential":
        agent = SequentialAgent(name="workflow", sub_agents=[agent])
    elif workflow == "tool":
        from ksadk.toolsets.a2a import call_a2a_agent

        os.environ["KSADK_A2A_SPACE_ID"] = request().spec.space_id
        agent = LlmAgent(name="tool_caller", model=ToolFixtureLlm(), tools=[call_a2a_agent])
    native_engine = None
    if schema := os.environ.get("R85_NATIVE_RECOVERY_SCHEMA"):
        native_engine = create_async_engine(
            os.environ["KSADK_A2A_TEST_DATABASE_URL"],
            connect_args={"server_settings": {"search_path": schema}},
        )
        native_sessions = DatabaseSessionService(db_engine=native_engine)
    else:
        native_sessions = DatabaseSessionService(
            db_url=f"sqlite+aiosqlite:///{directory / 'native.sqlite'}"
        )
    native = Runner(
        app=App(
            name="fixture",
            root_agent=agent,
            resumability_config=ResumabilityConfig(is_resumable=True),
        ),
        session_service=native_sessions,
    )
    if stage == "start":
        await native_sessions.create_session(
            app_name="fixture", user_id="user", session_id=native_session_id
        )
        kwargs = {"new_message": types.Content(role="user", parts=[types.Part(text="hello")])}
    else:
        previous = json.loads(state_path.read_text())
        if previous["remote_binding"] is not None:
            from ksadk.a2a.control_plane import RemoteTaskReference

            wire.binding = RemoteTaskReference(*previous["remote_binding"])
        kwargs = {"invocation_id": previous["native_invocation_id"]}
    output = []
    with bind_run_event_authority(authority), bind_managed_a2a(service, provider):
        native_events = native.run_async(user_id="user", session_id=native_session_id, **kwargs)
        if workflow == "tool":
            from ksadk.a2a.adk_runtime import managed_native_events

            async def discover():
                return [
                    SimpleNamespace(
                        agent_id=request().spec.target_agent_id,
                        version_id=request().spec.expected_version_id,
                        agent_card=SimpleNamespace(name="expert"),
                    )
                ]

            bootstrap = SimpleNamespace(
                outbound_enabled=True,
                runtime_metadata=SimpleNamespace(agent_id="caller"),
                client_for_space=lambda _: SimpleNamespace(discover=discover),
                invocation_service_for_space=lambda _space, **_deps: service,
            )
            native_events = managed_native_events(
                SimpleNamespace(_agent=agent, _managed_a2a_bootstrap=bootstrap), native_events
            )
        mapper, map_context = ADKEventAdapter(), ADKAdapterContext(run_id="public-run")
        async for event in native_events:
            canonical = canonical_event_from_adk(event)
            if canonical is None:
                if workflow == "tool":
                    for fact in mapper.map(event, map_context):
                        await authority.persist_one("s", fact)
                continue
            if stage == "start" and canonical.event_type == "interaction.requested":
                if workflow == "tool":
                    initial = (
                        canonical._invocation
                        if pause == "approval"
                        else await service.original_request(canonical._managed_input_waiter.handle)
                    )
                state_path.write_text(
                    json.dumps(
                        {
                            "native_invocation_id": event.invocation_id,
                            "request": initial.model_dump(mode="json"),
                            "remote_binding": (
                                [wire.binding.remote_task_id, wire.binding.remote_context_id]
                                if wire.binding is not None
                                else None
                            ),
                            "sends": [
                                message.message_id for kind, message in wire.calls if kind == "send"
                            ],
                        }
                    )
                )
                assert sum(kind == "send" for kind, _ in wire.calls) == (
                    1 if pause == "input" else 0
                )
                # Abrupt process death: no generator finalizers or remote cancel.
                os._exit(23)
            if stage == "resume" and canonical.event_type == "interaction.requested":
                assert pause == "input"
                wire.task, wire.events = completed, [completed]
                canonical._managed_input_waiter.deliver(
                    InteractionReceipt(
                        interaction_id=canonical.interaction_id,
                        revision=2,
                        status="resolved",
                        outcome="submitted",
                    ),
                    "resumed answer",
                )
            output.append(canonical.model_dump(mode="json"))
        if workflow == "tool" and stage == "resume":
            initial = await service.original_request(
                A2AInvocationRequest.model_validate(previous["request"]).handle
            )
    session = await native_sessions.get_session(
        app_name="fixture", user_id="user", session_id=native_session_id
    )
    report = {
        "output": output,
        "sends": [message.message_id for kind, message in wire.calls if kind == "send"],
        "sent_tasks": [message.task_id for kind, message in wire.calls if kind == "send"],
        "request": initial.model_dump(mode="json"),
        "native_invocations": sorted({event.invocation_id for event in session.events}),
        "user_messages": sum(event.author == "user" for event in session.events),
    }
    (directory / "recovered.json").write_text(json.dumps(report))
    await engine.dispose()
    if native_engine is not None:
        await native_engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())

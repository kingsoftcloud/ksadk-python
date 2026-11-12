"""Real ADK sequential caller for the local Server/Gateway network gate.

Only the model and local allow policy are fixtures. The native workflow,
remote_agent, invocation context, transport and remote producer are real.
Kernel lease/approval takeover is covered by the separate PostgreSQL matrix.
"""

from google.adk.agents import LlmAgent, SequentialAgent
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_response import LlmResponse
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from ksadk.a2a.adk import bind_managed_a2a, canonical_event_from_adk, remote_agent
from ksadk.a2a.adk_invocation_context import ADKInvocationContextProvider
from ksadk.a2a.invocation_policy import A2AInvocationPolicy, A2AInvocationPolicyDecision
from ksadk.a2a.invocation_service import A2AInvocationService
from ksadk.events.run_authority import RunEventAuthority, bind_run_event_authority
from ksadk.events.session_event import SessionServiceEventStore
from ksadk.kernel.contracts import ActivationWriteGuard


async def run_native_caller(wire, spec, sessions):
    session_id = "native-network-caller"
    await sessions.create_session("caller", "user", session_id)
    authority = RunEventAuthority(
        SessionServiceEventStore(sessions),
        session_id=session_id,
        run_id="native-network-run",
        guard=ActivationWriteGuard(activation_id="network", fencing_token=1),
    )
    service = A2AInvocationService.from_space_client(
        wire,
        event_store=authority,
        policy=A2AInvocationPolicy(evaluator=lambda *_: A2AInvocationPolicyDecision("allow")),
    )
    provider = ADKInvocationContextProvider(
        authority=authority,
        service_for_space=lambda _: service,
        caller_agent_id="caller",
        message_selector=lambda *_: ("hello",),
    )

    class ContinueModel(BaseLlm):
        model: str = "local-network-continuation"

        async def generate_content_async(self, llm_request, stream=False):
            assert any(
                "second answer" in (part.text or "")
                for content in llm_request.contents
                for part in content.parts
            ), "the next native agent must receive the remote result"
            yield LlmResponse(
                content=types.Content(
                    role="model", parts=[types.Part(text="native root continued")]
                )
            )

    agent = SequentialAgent(
        name="native_caller",
        sub_agents=[remote_agent(spec), LlmAgent(name="continue_root", model=ContinueModel())],
    )
    native_sessions = InMemorySessionService()
    await native_sessions.create_session(app_name="network", user_id="user", session_id=session_id)
    runner = Runner(agent=agent, app_name="network", session_service=native_sessions)
    observed, text = [], []
    with bind_run_event_authority(authority), bind_managed_a2a(service, provider):
        async for event in runner.run_async(
            user_id="user",
            session_id=session_id,
            new_message=types.Content(role="user", parts=[types.Part(text="hello")]),
        ):
            canonical = canonical_event_from_adk(event)
            if canonical is not None:
                observed.append(canonical.model_dump(mode="json"))
            if event.author == "continue_root" and event.content:
                text.extend(part.text for part in event.content.parts if part.text)
        assert text == ["native root continued"]
        descriptors = [
            event["snapshot"]["parts"][0]["data"]
            for event in observed
            if event["event_type"] == "item.completed" and event.get("item_kind") == "status"
        ]
        assert len(descriptors) == 1 and descriptors[0]["status"] == "completed"
        await wire.event_dispatcher.drain(raise_on_error=True)
    return {"canonical": observed, "root_output": text}

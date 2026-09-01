"""Browser acceptance for canonical ConversationItem presentation surfaces."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.request import urlopen

from playwright.sync_api import Page, Response, expect, sync_playwright
from studio_e2e_support import studio_server

from ksadk.events.canonical import (
    ApprovalRequest,
    ApprovalResponse,
    ContentSnapshot,
    InteractionRequested,
    InteractionResolved,
    ItemCompleted,
    ItemStarted,
    ItemUpdated,
    OutputRef,
    RunCompleted,
    RunInterrupted,
    RunStarted,
    RuntimeEvent,
    SourceRef,
    StructuredInputRequest,
    StructuredInputResponse,
    UsageReported,
)
from ksadk.events.content import (
    DataContent,
    TextContent,
    ToolCallContent,
    ToolResultContent,
)
from ksadk.runtime import RunHandle, StartRequest
from ksadk.studio.contracts import (
    AgentSpec,
    Instructions,
    ModelSpec,
    NetworkPolicy,
    RuntimeRef,
    SecuritySpec,
)
from ksadk.studio.service import StudioService
from tests.studio.runtime_adapter_fixtures import RuntimeFixture

AGENT_ID = "conversation-items-agent"
AGENT_NAME = "Conversation Items Agent"
REASONING = "核对运行条件"
TOOL_OUTPUT = "tool-result-ok"
SAME_BODY = "相同正文"


def _runtime_inspector(_runtime: object) -> tuple[str, str, str]:
    return "0.8.2", "0.147.0", "codex-cli 0.147.0"


def _seed_agent(service: StudioService) -> None:
    draft = service.create_studio_agent(
        agent_id=AGENT_ID,
        name=AGENT_NAME,
        spec=AgentSpec(
            runtime=RuntimeRef(type="codex", version="0.147.0"),
            model=ModelSpec(
                model="model-example",
                endpoint_url="https://model.example.com/v1/chat/completions",
                credential_ref="env://MODEL_API_KEY",
            ),
            instructions=Instructions(system="Render every canonical item by identity."),
            security=SecuritySpec(network=NetworkPolicy(allowed_hosts=["model.example.com"])),
        ),
    )
    service.builder.build(draft)


def _json(base_url: str, path: str) -> dict:
    with urlopen(f"{base_url}{path}") as response:
        return json.loads(response.read())


def _sse_events(base_url: str, path: str) -> list[dict]:
    with urlopen(f"{base_url}{path}") as response:
        body = response.read().decode("utf-8")
    events: list[dict] = []
    for block in body.replace("\r\n", "\n").split("\n\n"):
        lines = block.splitlines()
        event_name = next(
            (line[6:].strip() for line in lines if line.startswith("event:")),
            "message",
        )
        data = [line[5:].lstrip() for line in lines if line.startswith("data:")]
        if data and "\n".join(data) != "[DONE]":
            payload = json.loads("\n".join(data))
            payload.setdefault("type", event_name)
            events.append(payload)
    return events


class CanonicalConversationEvents:
    """Three stream phases separated by real approval and form resumes."""

    def __init__(self) -> None:
        self.phases: dict[str, int] = {}

    async def __call__(
        self,
        _request: StartRequest,
        handle: RunHandle,
    ) -> AsyncIterator[RuntimeEvent]:
        phase = self.phases.get(handle.run_id, 0)
        self.phases[handle.run_id] = phase + 1
        common = {
            "schema_version": 2,
            "timestamp": float(phase + 1),
            "run_id": handle.run_id,
            "scope_id": f"scope-{handle.run_id}",
        }

        def event_id(seq: int) -> str:
            return f"{handle.run_id}:e{seq}"

        codex = SourceRef(framework="codex")
        if phase == 0:
            yield RunStarted(
                event_id=event_id(1),
                seq=1,
                status="running",
                source=codex,
                **common,
            )
            yield ItemUpdated(
                event_id=event_id(2),
                seq=2,
                item_id="reasoning-1",
                item_kind="reasoning",
                op="append",
                update=TextContent(part_id="reasoning-text", text=REASONING),
                source=codex,
                **common,
            )
            tool_args = {"command": "echo safe", "cwd": "/workspace"}
            yield ItemStarted(
                event_id=event_id(3),
                seq=3,
                item_id="tool-1",
                item_kind="tool_call",
                initial=ContentSnapshot(
                    parts=(
                        ToolCallContent(
                            part_id="tool-call",
                            call_id="call-1",
                            name="codex.command",
                            arguments=tool_args,
                        ),
                    )
                ),
                source=codex,
                **common,
            )
            yield ItemCompleted(
                event_id=event_id(4),
                seq=4,
                item_id="tool-1",
                item_kind="tool_call",
                snapshot=ContentSnapshot(
                    parts=(
                        ToolCallContent(
                            part_id="tool-call",
                            call_id="call-1",
                            name="codex.command",
                            arguments=tool_args,
                        ),
                        ToolResultContent(
                            part_id="tool-result",
                            call_id="call-1",
                            result={
                                "status": "completed",
                                "exit_code": 0,
                                "output": TOOL_OUTPUT,
                            },
                        ),
                    )
                ),
                source=codex,
                **common,
            )
            yield InteractionRequested(
                event_id=event_id(5),
                seq=5,
                interaction_id="approval-1",
                interaction_kind="approval",
                request=ApprovalRequest(
                    call_id="call-1",
                    kind="command",
                    detail={"command": "echo safe"},
                ),
                source=codex,
                **common,
            )
            yield RunInterrupted(
                event_id=event_id(6),
                seq=6,
                status="interrupted",
                reason="approval",
                interaction_id="approval-1",
                source=codex,
                **common,
            )
            return

        if phase == 1:
            yield InteractionResolved(
                event_id=event_id(7),
                seq=7,
                interaction_id="approval-1",
                interaction_kind="approval",
                response=ApprovalResponse(decision="approved"),
                source=codex,
                **common,
            )
            surface_id = "profile-form"
            a2ui = SourceRef(
                framework="codex",
                protocol="a2ui",
                metadata={"surface_id": surface_id},
            )
            operations = [
                {
                    "version": "v0.9",
                    "createSurface": {
                        "surfaceId": surface_id,
                        "catalogId": "https://a2ui.org/specification/v0_9/basic_catalog.json",
                    },
                },
                {
                    "version": "v0.9",
                    "updateComponents": {
                        "surfaceId": surface_id,
                        "components": [
                            {
                                "id": "profile",
                                "component": "Form",
                                "title": "补充资料",
                                "children": ["name"],
                                "submit_label": "提交资料",
                            },
                            {
                                "id": "name",
                                "component": "TextField",
                                "name": "name",
                                "label": "姓名",
                            },
                        ],
                    },
                },
                {
                    "version": "v0.9",
                    "updateDataModel": {
                        "surfaceId": surface_id,
                        "path": "/",
                        "value": {"name": ""},
                    },
                },
            ]
            yield ItemStarted(
                event_id=event_id(8),
                seq=8,
                item_id="a2ui-profile",
                item_kind="data",
                initial=ContentSnapshot(parts=(DataContent(part_id="a2ui-data", data=operations),)),
                source=a2ui,
                **common,
            )
            yield InteractionRequested(
                event_id=event_id(9),
                seq=9,
                interaction_id="form-1",
                interaction_kind="structured_input",
                request=StructuredInputRequest(
                    prompt="请补充姓名",
                    schema={
                        "type": "object",
                        "properties": {"name": {"type": "string"}},
                        "required": ["name"],
                    },
                ),
                source=a2ui,
                **common,
            )
            yield RunInterrupted(
                event_id=event_id(10),
                seq=10,
                status="interrupted",
                reason="structured_input",
                interaction_id="form-1",
                source=codex,
                **common,
            )
            return

        if phase == 2:
            a2ui = SourceRef(
                framework="codex",
                protocol="a2ui",
                metadata={"surface_id": "profile-form"},
            )
            yield InteractionResolved(
                event_id=event_id(11),
                seq=11,
                interaction_id="form-1",
                interaction_kind="structured_input",
                response=StructuredInputResponse(data={"name": "Alice"}),
                source=a2ui,
                **common,
            )
            yield UsageReported(
                event_id=event_id(12),
                seq=12,
                input_tokens=5,
                output_tokens=3,
                total_tokens=8,
                source=codex,
                **common,
            )
            for offset, item_id in enumerate(("same-1", "same-2"), start=0):
                start_seq = 13 + (offset * 3)
                yield ItemStarted(
                    event_id=event_id(start_seq),
                    seq=start_seq,
                    item_id=item_id,
                    item_kind="message",
                    phase="final_answer",
                    initial=ContentSnapshot(parts=()),
                    source=codex,
                    **common,
                )
                yield ItemUpdated(
                    event_id=event_id(start_seq + 1),
                    seq=start_seq + 1,
                    item_id=item_id,
                    item_kind="message",
                    op="append",
                    update=TextContent(
                        part_id=f"{item_id}-text",
                        text=f"{SAME_BODY}\n\n",
                    ),
                    source=codex,
                    **common,
                )
                yield ItemCompleted(
                    event_id=event_id(start_seq + 2),
                    seq=start_seq + 2,
                    item_id=item_id,
                    item_kind="message",
                    snapshot=ContentSnapshot(
                        parts=(
                            TextContent(
                                part_id=f"{item_id}-text",
                                text=f"{SAME_BODY}\n\n",
                            ),
                        )
                    ),
                    source=codex,
                    **common,
                )
            # Keep both identity-distinct items visible in the live production
            # surface long enough for the browser assertion before terminal
            # persistence replaces the streaming turn.
            await asyncio.sleep(1.0)
            yield RunCompleted(
                event_id=event_id(19),
                seq=19,
                status="completed",
                output_refs=(
                    OutputRef(
                        scope_id=common["scope_id"],
                        item_id="same-1",
                        part_id="same-1-text",
                    ),
                    OutputRef(
                        scope_id=common["scope_id"],
                        item_id="same-2",
                        part_id="same-2-text",
                    ),
                ),
                source=SourceRef(
                    framework="codex",
                    metadata={"duration_ms": 25},
                ),
                **common,
            )
            return

        raise AssertionError(f"unexpected fourth runtime stream phase for {handle.run_id}")


def _exercise_conversation_items(page: Page, second_page: Page, base_url: str) -> None:
    interaction_responses: list[tuple[str, int]] = []
    interaction_payloads: list[dict] = []

    def record_response(response: Response) -> None:
        if "/interactions/" in response.url and response.url.endswith(":submit"):
            interaction_responses.append((response.url, response.status))
            interaction_payloads.append(response.request.post_data_json)

    page.on("response", record_response)
    second_page.on("response", record_response)
    # Studio owns long-lived/polling surfaces, so browser readiness is the
    # rendered conversation contract rather than a global network-idle gap.
    page.goto(f"{base_url}/#/conversations", wait_until="domcontentloaded")
    expect(page.get_by_role("combobox", name="切换会话目标")).to_contain_text(AGENT_NAME)
    composer = page.get_by_role("textbox", name="消息")
    expect(composer).to_be_enabled()
    composer.fill("展示 canonical 会话项目")
    page.get_by_role("button", name="发送消息").click()

    thinking = page.locator('details[data-ui="think"]')
    expect(thinking).to_contain_text(REASONING, timeout=15_000)
    # Typed ConversationItems keep their stream order: the tool is its own
    # card after the reasoning block rather than being folded into thinking.
    tool_card = page.locator(".chat-activity-card.tool")
    expect(tool_card).to_contain_text("codex.command")
    expect(tool_card).to_contain_text(TOOL_OUTPUT)

    approval_tray = page.locator('[data-ui="interaction-tray"]')
    expect(approval_tray).to_contain_text("echo safe")
    second_page.goto(f"{base_url}/#/conversations", wait_until="domcontentloaded")
    second_approval_tray = second_page.locator('[data-ui="interaction-tray"]')
    expect(second_approval_tray).to_contain_text("echo safe", timeout=15_000)
    # Two Studio windows submit the same authoritative revision.  Both receive
    # the persisted receipt while the provider observes exactly one resume.
    approval_tray.get_by_role("button", name="允许").evaluate("element => element.click()")
    active_run_id = _json(base_url, "/api/v1/runs")["items"][0]["id"]
    second_page.evaluate(
        """({ runId }) => {
          window.__interactionReplayStatus = undefined;
          void fetch(`/api/v1/runs/${encodeURIComponent(runId)}/interactions/approval-1:submit`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              name: "approve",
              data: {},
              expectedRevision: 1,
              idempotencyKey: "interaction:approval-1:revision-1",
            }),
          }).then(response => { window.__interactionReplayStatus = response.status; });
        }""",
        {"runId": active_run_id},
    )
    second_page.wait_for_function("window.__interactionReplayStatus !== undefined")
    assert second_page.evaluate("window.__interactionReplayStatus") == 200

    form_tray = page.locator('[data-ui="interaction-tray"]')
    name_input = form_tray.get_by_role("textbox", name="姓名")
    expect(name_input).to_be_visible(timeout=15_000)
    name_input.fill("Alice")
    form_tray.get_by_role("button", name="提交资料").click()

    # Unhandled/additive provider items must degrade silently: no
    # "unsupported content" fallback card may ever appear in the chat
    # surface.  Unknown kinds stay observable via the raw RuntimeEvent
    # stream (and are pinned hidden in tests/conversations).
    fallback = page.locator('[data-ui="conversation-fallback"]')
    expect(fallback).to_have_count(0)

    # Equal text from two canonical Items must remain two rendered blocks.
    # Text-based deduplication would incorrectly collapse this to one.
    equal_paragraphs = page.locator('article[data-role="assistant"] .chat-markdown p').filter(
        has_text=SAME_BODY
    )
    expect(equal_paragraphs).to_have_count(2, timeout=15_000)

    # A terminal canonical Run must replace the optimistic streaming turn.
    expect(page.locator(".streaming-turn")).to_have_count(0, timeout=10_000)
    # Terminal persistence must preserve both identity-distinct outputs instead
    # of replacing them with the last completed message snapshot.
    expect(equal_paragraphs).to_have_count(2)
    assert len(interaction_responses) == 3
    assert all(status == 200 for _url, status in interaction_responses)
    assert all(payload["expectedRevision"] == 1 for payload in interaction_payloads)
    assert {payload["idempotencyKey"] for payload in interaction_payloads} == {
        "interaction:approval-1:revision-1",
        "interaction:form-1:revision-1",
    }

    runs = _json(base_url, "/api/v1/runs")["items"]
    assert len(runs) == 1
    events = _sse_events(base_url, f"/api/v1/runs/{runs[0]['id']}/events")
    runtime_events = [
        event["runtimeEvent"] for event in events if isinstance(event.get("runtimeEvent"), dict)
    ]
    assert {event["event_type"] for event in runtime_events} >= {
        "item.updated",
        "item.started",
        "item.completed",
        "interaction.requested",
        "interaction.resolved",
        "usage.reported",
        "run.completed",
    }
    same_items = [
        event
        for event in runtime_events
        if event.get("event_type") == "item.completed"
        and event.get("item_id") in {"same-1", "same-2"}
    ]
    assert {event["item_id"] for event in same_items} == {"same-1", "same-2"}
    assert {event["snapshot"]["parts"][0]["text"] for event in same_items} == {f"{SAME_BODY}\n\n"}
    assert {event["event_type"] for event in runtime_events}.issuperset(
        {"interaction.requested", "interaction.resolved"}
    )
    assert sum(event["event_type"] == "interaction.requested" for event in runtime_events) == 2
    assert sum(event["event_type"] == "interaction.resolved" for event in runtime_events) == 2
    actions = [event for event in events if event.get("type") == "a2ui.action"]
    # Runtime InteractionResolved also projects an additive a2ui.action
    # without the Studio submit receipt's action name.
    assert {"approve", "submit"}.issubset({event.get("name") for event in actions}), actions
    assert any(event.get("data") == {"name": "Alice"} for event in actions)


def main() -> None:
    event_stream = CanonicalConversationEvents()
    fixture = RuntimeFixture(event_stream)
    with TemporaryDirectory(prefix="ksadk-conversation-items-") as temp_dir:
        workspace = Path(temp_dir)
        service = StudioService(
            workspace,
            codex_runtime_inspector=_runtime_inspector,
            runtime_executor=fixture.executor,
        )
        _seed_agent(service)
        with (
            studio_server(workspace, service=service) as base_url,
            sync_playwright() as playwright,
        ):
            browser = playwright.chromium.launch(headless=True)
            try:
                context = browser.new_context(viewport={"width": 1440, "height": 960})
                page = context.new_page()
                second_page = context.new_page()
                page_errors: list[str] = []
                page.on("pageerror", lambda error: page_errors.append(str(error)))
                second_page.on("pageerror", lambda error: page_errors.append(str(error)))
                _exercise_conversation_items(page, second_page, base_url)
                assert page_errors == [], f"Uncaught React page errors: {page_errors}"
            finally:
                browser.close()

    assert len(fixture.start_requests) == 1
    assert list(event_stream.phases.values()) == [3]


if __name__ == "__main__":
    main()

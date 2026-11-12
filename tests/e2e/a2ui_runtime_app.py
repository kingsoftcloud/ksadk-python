"""Real local Runtime used by Chromium tests; no API/SSE response stubs.

Launch with uvicorn tests.e2e.a2ui_runtime_app:create_app --factory. Test-only
environment selects a fresh persistent project and an ordinary Web build.
"""

import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import TypedDict
from uuid import uuid4

from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode
from langgraph.types import interrupt

from ksadk.a2ui import build_a2ui_presentation
from ksadk.a2ui.catalog import catalog_manifest
from ksadk.agui.config import AGUIConfig
from ksadk.interaction.langgraph import request_input
from ksadk.runners.langgraph_runner import LangGraphRunner
from ksadk.runtime.adapter import RuntimeLaunchContext, RuntimeRegistry, RuntimeServices
from ksadk.runtime.executor import RuntimeExecutor
from ksadk.runtime.factory import create_runtime_adapter
from ksadk.server.composition import configure_runtime_app
from ksadk.server.factory import RuntimeAppConfig, create_runtime_app
from ksadk.sessions.local_service import LocalSessionService


class State(TypedDict, total=False):
    messages: list
    context: dict
    presentation: dict
    surface_id: str
    answer: str


AGENTS = {
    "resource": ("Resource query", "resource", "Resource identifier"),
    "trip": ("Trip plan", "destination", "Destination"),
    "ticket": ("Create a ticket", "subject", "Ticket subject"),
}


def approval_operations(surface_id, title, *, legacy=False, mapped=True):
    """Different layouts, the same explicit approve/reject contract; no cloud writes."""
    from ksadk.a2ui.models import BASIC_CATALOG_ID

    def op(**value):
        return {"version": "v0.9.1", **value}

    if legacy:
        components = [
            {"id": "details", "component": "Text", "text": title},
            {"id": "decision", "component": "ApprovalBar", "summary": "Local operation",
             "approve_label": "确认执行", "deny_label": "取消"},
        ]
    else:
        components = [
            {"id": "root", "component": "Column", "children": ["details", "yes", "no"]},
            {"id": "details", "component": "Text", "text": title},
            {"id": "yes", "component": "Button", "child": "yes-label",
             "action": {"event": {"name": "approve" if mapped else "submit"}}},
            {"id": "yes-label", "component": "Text", "text": "允许本地操作"},
            {"id": "no", "component": "Button", "child": "no-label",
             "action": {"event": {"name": "reject" if mapped else "cancel"}}},
            {"id": "no-label", "component": "Text", "text": "拒绝本地操作"},
        ]
    return [
        op(createSurface={"surfaceId": surface_id, "catalogId": (
            BASIC_CATALOG_ID if legacy else catalog_manifest()["catalog_id"])}),
        op(updateComponents={"surfaceId": surface_id, "components": components}),
    ]


def create_app():
    project = Path(os.environ["KSADK_TEST_A2UI_PROJECT"]).resolve()
    project.mkdir(parents=True, exist_ok=True)
    web = Path(os.environ["KSADK_TEST_A2UI_WEB"]).resolve()
    assert (web / "index.html").is_file()
    title, field, label = AGENTS[os.getenv("KSADK_TEST_A2UI_AGENT", "resource")]
    os.environ["KSADK_UI_PROFILE"] = "custom"
    os.environ["KSADK_UI_PATH"] = os.getenv("KSADK_TEST_A2UI_UI_PATH", "/")
    os.environ["KSADK_UI_BUNDLE_PATH"] = str(web)
    os.environ["AGENTENGINE_UI_DIR"] = str(project / ".agentengine/ui")
    schema = {
        "type": "object",
        "properties": {
            field: {
                "type": "string",
                "minLength": 3,
                "title": label,
            }
        },
        "required": [field],
        "additionalProperties": False,
    }
    evidence = project / "execution.jsonl"
    interrupt_mode = os.getenv("KSADK_TEST_A2UI_INTERRUPT_MODE")
    custom_mode = os.getenv("KSADK_TEST_A2UI_CUSTOM_CATALOG")
    custom_document = None
    custom_registry = None
    if custom_mode:
        from ksadk.a2ui import A2uiCatalogRegistry

        catalog_fixture = Path(__file__).parents[1] / "fixtures" / "a2ui" / "custom_catalog.json"
        custom_document = json.loads(catalog_fixture.read_text())
        custom_registry = A2uiCatalogRegistry([custom_document])
        schema["properties"].update({
            "confirmed": {"type": "boolean", "title": "Confirm details"},
            "tier": {"type": "array", "items": {"enum": ["standard", "urgent"]}},
            "secret": {"type": "string", "writeOnly": True},
        })
        schema["required"].extend(["confirmed", "tier"])

    def record(phase, **details):
        with evidence.open("a") as output:
            output.write(json.dumps({"phase": phase, **details}) + "\n")

    def initialize(_state, config: RunnableConfig):
        record("initialize", thread_id=config.get("configurable", {}).get("thread_id"))
        return {"context": {"private": "sdk-internal-context-must-not-be-chat"}}

    async def layout(_state):
        sid = "card-" + uuid4().hex
        if interrupt_mode in {"approval-a2ui", "approval-legacy", "approval-preview"}:
            operations = approval_operations(
                sid, title, legacy=interrupt_mode == "approval-legacy",
                mapped=interrupt_mode != "approval-preview",
            )
            return {"surface_id": sid,
                    "presentation": build_a2ui_presentation(operations, title=title)}
        if (
            os.getenv("KSADK_TEST_A2UI_PRESENTATION") == "schema"
            or interrupt_mode in {"untyped", "approval"}
        ):
            return {"surface_id": sid, "presentation": {"title": title}}

        def op(**value):
            return {"version": "v0.9.1", **value}

        operations = [
            op(createSurface={"surfaceId": sid, "catalogId": catalog_manifest()["catalog_id"]}),
            op(
                updateComponents={
                    "surfaceId": sid,
                    "components": [
                        {
                            "id": "root",
                            "component": "Column",
                            "children": ["title", "field", "submit"],
                        },
                        {"id": "title", "component": "Text", "text": title},
                        {
                            "id": "field",
                            "component": "TextField",
                            "label": label,
                            "value": {"path": f"/answer/{field}"},
                        },
                        {
                            "id": "submit",
                            "component": "Button",
                            "child": "submit-label",
                            "action": {
                                "event": {
                                    "name": "submit",
                                    "context": {
                                        "payload": {"path": "/answer"},
                                    },
                                }
                            },
                        },
                        {"id": "submit-label", "component": "Text", "text": "Submit request"},
                    ],
                }
            ),
            op(updateDataModel={"surfaceId": sid, "path": "/", "value": {"answer": {field: ""}}}),
        ]
        if custom_document:
            operations[0]["createSurface"]["catalogId"] = custom_document["catalogId"]
            entries = operations[1]["updateComponents"]["components"]
            entries[0]["children"] = ["title", "field", "confirmed", "tier", "secret", "submit"]
            entries[2]["component"] = "FeedbackInput"
            entries.extend([
                {"id": "confirmed", "component": "CheckBox", "label": "Confirm details",
                 "value": {"path": "/answer/confirmed"}},
                {"id": "tier", "component": "ChoicePicker", "label": "Priority",
                 "options": [{"label": "Standard", "value": "standard"}, {"label": "Urgent", "value": "urgent"}],
                 "value": {"path": "/answer/tier"}},
                {"id": "secret", "component": "FeedbackInput", "label": "Private note",
                 "variant": "obscured", "value": {"path": "/answer/secret"}},
            ])
            operations[2]["updateDataModel"]["value"]["answer"].update(confirmed=False, tier=[], secret="")
        if os.getenv("KSADK_TEST_A2UI_BAD_CATALOG") == "1":
            operations[0]["createSurface"]["catalogId"] = "https://example.invalid/unregistered"
        if model_env := os.getenv("KSADK_TEST_A2UI_MODEL_ENV"):
            from dotenv import dotenv_values
            from langchain_core.messages import HumanMessage
            from langchain_openai import ChatOpenAI

            from ksadk.a2ui.catalog import catalog_document
            from ksadk.compat.copilotkit_a2ui import (
                _KSADK_HOSTED_UI_GUIDELINES,
                _generate_a2ui_envelope,
            )

            values = dotenv_values(model_env)
            model = ChatOpenAI(
                model=values["OPENAI_MODEL_NAME"],
                api_key=values["OPENAI_API_KEY"],
                base_url=values["OPENAI_BASE_URL"],
                temperature=0,
                timeout=90,
                max_retries=0,
            )
            raw = await _generate_a2ui_envelope(
                config={
                    "model": model,
                    "default_catalog_id": catalog_manifest()["catalog_id"],
                    "catalog": catalog_document(),
                    "default_surface_id": sid,
                    "guidelines": _KSADK_HOSTED_UI_GUIDELINES,
                    "recovery": {"maxAttempts": 3},
                },
                state={},
                messages=[
                    HumanMessage(
                        content=(
                            f"Create an input form for {title}. "
                            f"Business JSON Schema: {json.dumps(schema)}. "
                            f"Use a TextField bound to /answer/{field} "
                            "and initialize that field to empty. "
                            "Include one Button with Text child 'Submit request', "
                            "event name submit, "
                            "context.payload bound to /answer. Use only the supplied catalog."
                        )
                    )
                ],
                intent="create",
                target_surface_id=None,
                changes=None,
            )
            generated = json.loads(raw)
            if "a2ui_operations" not in generated:
                raise ValueError("Real model failed registered catalog validation")
            operations = generated["a2ui_operations"]
            sid = next(
                op["createSurface"]["surfaceId"] for op in operations if "createSurface" in op
            )
            record("model_layout", operation_count=len(operations))
        return {
            "surface_id": sid,
            "presentation": build_a2ui_presentation(
                operations, title=title, catalog_registry=custom_registry,
                catalog_digest=(custom_registry.describe(custom_document["catalogId"])["catalog_digest"]
                                if custom_document else catalog_manifest()["catalog_digest"]),
            ),
        }

    def wait(state):
        if interrupt_mode and interrupt_mode.startswith("approval"):
            value = {"kind": "approval", "message": title}
            if interrupt_mode != "approval":
                value["presentation"] = state["presentation"]
            decision = interrupt(value)
            record("native_input", answer=decision)
            if decision.get("decision") != "approve":
                return {"answer": "rejected", "messages": [AIMessage(content="Request rejected")]}
            answer = {field: "approved"}
        elif interrupt_mode in {"untyped", "untyped-a2ui"}:
            value = {"type": "custom_collect", "prompt": title}
            if interrupt_mode == "untyped-a2ui":
                value["presentation"] = state["presentation"]
            result = interrupt(value)
            record("native_input", answer=result)
            answer = result if interrupt_mode == "untyped-a2ui" else {field: result}
        else:
            answer = request_input(title, schema, presentation=state["presentation"])
        return {
            "answer": str(answer[field]),
            "messages": [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "complete_request",
                            "id": "complete-" + state["surface_id"],
                            "args": {"answer": answer, "surface_id": state["surface_id"]},
                        }
                    ],
                )
            ]
        }

    @tool
    async def complete_request(answer: dict, surface_id: str) -> dict:
        """Record a synthetic business operation after human input."""
        if delay := os.getenv("KSADK_TEST_A2UI_TOOL_DELAY"):
            import asyncio

            await asyncio.sleep(float(delay))
        if os.getenv("KSADK_TEST_A2UI_MCP") == "1":
            import sys

            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client

            params = StdioServerParameters(
                command=sys.executable,
                args=[
                    str(Path(__file__).with_name("a2ui_mcp_server.py")),
                ],
            )
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    result = await session.call_tool(
                        "complete_request",
                        {
                            "answer": answer,
                            "field": field,
                            "surface_id": surface_id,
                            "evidence": str(evidence),
                        },
                    )
                    if result.isError:
                        raise RuntimeError("Local MCP business fixture rejected the request")
                    data = result.structuredContent
                    if data is None:
                        data = json.loads(
                            next(part.text for part in result.content if part.type == "text")
                        )
            record("tool", answer=answer, surface_id=surface_id)
            return data
        record("tool", answer=answer, surface_id=surface_id)
        if (
            os.getenv("KSADK_TEST_A2UI_PRESENTATION") == "schema"
            or os.getenv("KSADK_TEST_A2UI_BAD_CATALOG") == "1"
            or interrupt_mode in {"untyped", "approval"}
            or bool(interrupt_mode and interrupt_mode.startswith("approval-"))
        ):
            return {"resource": answer[field], "source": "local test fixture"}
        return {
            "a2ui_operations": [
                {
                    "version": "v0.9.1",
                    "updateComponents": {
                        "surfaceId": surface_id,
                        "components": [
                            {
                                "id": "root",
                                "component": "Text",
                                "text": "Completed: " + str(answer[field]),
                            }
                        ],
                    },
                }
            ]
        }

    def finish(state):
        if interrupt_mode and interrupt_mode.startswith("approval") and state["answer"] == "rejected":
            return {
                "answer": "Request rejected", "messages": [AIMessage(content="Request rejected")]
            }
        if (
            os.getenv("KSADK_TEST_A2UI_PRESENTATION") == "schema"
            or os.getenv("KSADK_TEST_A2UI_BAD_CATALOG") == "1"
            or interrupt_mode in {"untyped", "approval"}
            or bool(interrupt_mode and interrupt_mode.startswith("approval-"))
        ):
            answer = "Completed: " + state["answer"]
            return {"answer": answer, "messages": [AIMessage(content=answer)]}
        return {"answer": "Request completed"}

    builder = StateGraph(State)
    wait_node = wait
    if os.getenv("KSADK_TEST_A2UI_SUBGRAPH") == "1":
        child = StateGraph(State)
        child.add_node("input", wait)
        child.add_edge(START, "input")
        child.add_edge("input", END)
        wait_node = child.compile()
    for name, node in [
        ("initialize", initialize),
        ("layout", layout),
        ("wait", wait_node),
        ("tools", ToolNode([complete_request])),
        ("finish", finish),
    ]:
        builder.add_node(name, node)
    for source, target in zip(
        [START, "initialize", "layout", "wait", "tools", "finish"],
        ["initialize", "layout", "wait", "tools", "finish", END],
        strict=True,
    ):
        builder.add_edge(source, target)
    detection = SimpleNamespace(name="a2ui-e2e", type=SimpleNamespace(value="langgraph"))
    runner = LangGraphRunner(detection, str(project))
    runner._agent = builder.compile()  # No saver: exercise the ordinary SDK-owned local lifecycle.
    runner._module = SimpleNamespace(
        ksadk_graph_factory=lambda *, checkpointer: builder.compile(checkpointer=checkpointer),
        ksadk_prepare_state=lambda payload, session_context: {"messages": payload.get("messages", [])},
    )
    registry = RuntimeRegistry()
    # Keep the same framework initialization (including model stream patches)
    # as CLI/platform launch; only the source of the test runner is injected.
    def make_adapter(context):
        adapter = create_runtime_adapter(context)
        # The fixture deliberately shares one injected runner across requests.
        # Match app-owned lifecycle rather than closing it after each activation.
        adapter.adopt_app_lifetime()
        return adapter

    registry.register("langgraph", make_adapter)
    if dsn := os.getenv("KSADK_TEST_A2UI_POSTGRES_DSN"):
        from ksadk.sessions.postgres_service import PostgresSessionService

        service = PostgresSessionService(dsn=dsn, namespace=os.getenv("KSADK_SESSION_NAMESPACE", "custom-catalog-e2e"))
    else:
        service = LocalSessionService(project_dir=str(project))
    app = create_runtime_app(
        RuntimeAppConfig(
            launch_context=RuntimeLaunchContext(
                runtime_type="langgraph",
                project_dir=project,
                detection=detection,
                services=RuntimeServices(runner_factory=lambda *_: runner),
            ),
            runtime_executor=RuntimeExecutor(registry),
            agui=AGUIConfig(enabled=True, agent_name="a2ui-e2e"),
            session_service_provider=lambda: service,
            a2ui_catalog_registry=custom_registry if custom_mode == "registered" else None,
        ),
        configure_runtime_app,
    )

    @app.post("/test/evidence")
    async def read_evidence():
        rows = (
            [json.loads(line) for line in evidence.read_text().splitlines()]
            if evidence.exists()
            else []
        )
        checkpoints = []
        threads = dict.fromkeys(row.get("thread_id") for row in rows if row.get("thread_id"))
        # Read durable evidence independently of an activation-owned adapter:
        # completed runs may legitimately close their saver connection.
        if dsn := os.getenv("KSADK_TEST_A2UI_POSTGRES_DSN"):
            from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

            async with AsyncPostgresSaver.from_conn_string(dsn) as saver:
                graph = builder.compile(checkpointer=saver)
                for thread in threads:
                    snapshot = await graph.aget_state({"configurable": {"thread_id": thread}})
                    if snapshot.config.get("configurable", {}).get("checkpoint_id"):
                        checkpoints.append({"config": snapshot.config, "next": list(snapshot.next)})
        elif getattr(runner._agent, "checkpointer", None) is not None:
            for thread in threads:
                snapshot, error = await runner._read_graph_state({"configurable": {"thread_id": thread}})
                if error is None and snapshot is not None:
                    checkpoints.append({"config": snapshot.config, "next": list(snapshot.next)})
        return {"executions": rows, "project": str(project), "native_checkpoints": checkpoints}

    return app

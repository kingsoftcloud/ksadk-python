#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import os
import uuid
from types import SimpleNamespace
from typing import Any

from google.adk.events.event import Event as ADKEvent
from google.adk.sessions import DatabaseSessionService
from google.genai import types

from ksadk.conversations.runtime import invoke_conversation_once
from ksadk.memory.adk.resilient_session_service import ResilientADKSessionService
from ksadk.runners.adk_runner import ADKRunner
from ksadk.sessions.base import SessionEvent
from ksadk.sessions.postgres_service import PostgresSessionService
from ksadk.sessions.resilient import ResilientSessionService


class _FrameworkRunner:
    def __init__(self, framework: str) -> None:
        self.framework = framework

    def prepare_for_request(self, _model: str | None) -> None:
        return None

    async def invoke(self, _payload: dict[str, Any]) -> dict[str, Any]:
        return {"output": f"{self.framework}:ok"}


class _ADKNativeRunner:
    def __init__(self, session_service: ResilientADKSessionService, app_name: str) -> None:
        self.session_service = session_service
        self.app_name = app_name

    async def run_async(self, *, session_id: str, user_id: str, **_kwargs: Any):
        session = await self.session_service.get_session(
            app_name=self.app_name,
            user_id=user_id,
            session_id=session_id,
        )
        if session is None:
            raise RuntimeError(f"ADK session {session_id} was not created")
        event = ADKEvent(
            author=self.app_name,
            invocation_id=f"inv-{uuid.uuid4().hex[:8]}",
            content=types.Content(role="model", parts=[types.Part(text="adk:ok")]),
        )
        await self.session_service.append_event(session, event)
        yield event


async def _run_canonical_framework(framework: str, unavailable_dsn: str) -> dict[str, Any]:
    service = ResilientSessionService(
        PostgresSessionService(dsn=unavailable_dsn, connect_timeout=0.2)
    )
    runner = _FrameworkRunner(framework)
    session_id, result = await invoke_conversation_once(
        runner=runner,
        agent_id=f"{framework}-e2e",
        user_id="preprod-e2e",
        session_id=f"sess-{framework}-{uuid.uuid4().hex[:8]}",
        messages=[{"role": "user", "content": "session fail-open check"}],
        model="e2e-model",
        prepare_runner=lambda active_runner, model: active_runner.prepare_for_request(model),
        session_service_provider=lambda: service,
    )
    events = await service.get_events(session_id)
    event_types = [event.event_type for event in events]
    expected = ["user_message", "run_status", "assistant_message", "run_status"]
    if result.get("output_text") != f"{framework}:ok" or event_types != expected:
        raise RuntimeError(
            f"{framework} fail-open mismatch: output={result.get('output_text')!r} "
            f"events={event_types!r}"
        )
    return {
        "framework": framework,
        "status": "pass",
        "degraded": service.degraded,
        "event_types": event_types,
    }


async def _run_adk_native(unavailable_dsn: str) -> dict[str, Any]:
    database = DatabaseSessionService(db_url=_sqlalchemy_dsn(unavailable_dsn))
    session_service = ResilientADKSessionService(database)
    detection = SimpleNamespace(
        name="adk-e2e",
        type=SimpleNamespace(value="adk"),
        entry_point="agent.py",
        agent_variable="root_agent",
    )
    runner = ADKRunner(detection, "/tmp")
    runner._agent = SimpleNamespace(name="adk-e2e")
    runner._session_service = session_service
    runner._runner = _ADKNativeRunner(session_service, "adk-e2e")

    result = await runner.invoke(
        {
            "input": "session fail-open check",
            "session_id": f"sess-adk-{uuid.uuid4().hex[:8]}",
        }
    )
    if result.get("output") != "adk:ok" or not session_service.degraded:
        raise RuntimeError(f"ADK native fail-open mismatch: {result!r}")
    await session_service.close()
    return {
        "framework": "adk",
        "status": "pass",
        "degraded": True,
        "output": result.get("output"),
    }


def _asyncpg_dsn(dsn: str) -> str:
    return dsn.replace("postgresql+asyncpg://", "postgresql://", 1)


def _sqlalchemy_dsn(dsn: str) -> str:
    if dsn.startswith("postgresql+asyncpg://"):
        return dsn
    return dsn.replace("postgresql://", "postgresql+asyncpg://", 1)


async def _validate_postgres_view(dsn: str) -> dict[str, Any]:
    import asyncpg

    namespace = f"preprod_e2e_{uuid.uuid4().hex[:8]}"
    session_id = f"sess-pg-{uuid.uuid4().hex[:8]}"
    service = PostgresSessionService(dsn=_asyncpg_dsn(dsn), namespace=namespace)
    try:
        await service.create_session("pg-e2e", "preprod-e2e", session_id=session_id)
        events = [
            SessionEvent(
                author="user",
                event_type="user_message",
                content={"role": "user", "parts": [{"text": "hello pg"}]},
            ),
            SessionEvent(
                author="pg-e2e",
                event_type="reasoning",
                content={"role": "assistant", "parts": [{"text": "one reasoning row"}]},
                invocation_id="inv-pg-e2e",
            ),
            SessionEvent(
                author="pg-e2e",
                event_type="assistant_message",
                content={"role": "assistant", "parts": [{"text": "hello user"}]},
                invocation_id="inv-pg-e2e",
            ),
            SessionEvent(
                author="pg-e2e",
                event_type="run_status",
                content={"status": "completed"},
                invocation_id="inv-pg-e2e",
            ),
        ]
        for event in events:
            await service.append_event(session_id, event)

        connection = await asyncpg.connect(_asyncpg_dsn(dsn))
        try:
            rows = await connection.fetch(
                """
                SELECT seq_id, event_type, message_role, message_text, lifecycle_status
                FROM ksadk_session_events_readable
                WHERE namespace = $1 AND session_id = $2
                ORDER BY seq_id
                """,
                namespace,
                session_id,
            )
        finally:
            await connection.close()
        event_types = [row["event_type"] for row in rows]
        if event_types != [
            "user_message",
            "reasoning",
            "assistant_message",
            "run_status",
        ]:
            raise RuntimeError(f"Readable PostgreSQL view mismatch: {event_types!r}")
        if rows[0]["message_text"] != "hello pg" or rows[-1]["lifecycle_status"] != "completed":
            raise RuntimeError("Readable PostgreSQL view did not flatten message/lifecycle fields")
        return {
            "status": "pass",
            "row_count": len(rows),
            "event_types": event_types,
            "reasoning_row_count": event_types.count("reasoning"),
        }
    finally:
        try:
            await service.delete_session(session_id)
        finally:
            await service.aclose()


async def run_validation(*, dsn: str, unavailable_dsn: str) -> dict[str, Any]:
    framework_results = [
        await _run_canonical_framework(framework, unavailable_dsn)
        for framework in ("langgraph", "langchain")
    ]
    framework_results.append(await _run_adk_native(unavailable_dsn))
    postgres_result = await _validate_postgres_view(dsn) if dsn else {"status": "skipped"}
    return {
        "overall_status": "pass",
        "frameworks": framework_results,
        "postgres_readability": postgres_result,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Validate fail-open session execution and readable PostgreSQL events."
    )
    parser.add_argument("--dsn", default=os.getenv("KSADK_TEST_POSTGRES_DSN", ""))
    parser.add_argument(
        "--unavailable-dsn",
        default="postgresql://ksadk@127.0.0.1:1/ksadk_failopen",
    )
    args = parser.parse_args()
    report = asyncio.run(
        run_validation(
            dsn=args.dsn.strip(),
            unavailable_dsn=args.unavailable_dsn.strip(),
        )
    )
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()

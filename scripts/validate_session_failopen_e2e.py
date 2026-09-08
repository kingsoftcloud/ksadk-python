#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import os
import uuid
from contextlib import suppress
from types import SimpleNamespace
from typing import Any
from urllib.parse import quote, unquote, urlsplit, urlunsplit

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


class _PostgresTcpProxy:
    def __init__(self, target_host: str, target_port: int) -> None:
        self.target_host = target_host
        self.target_port = target_port
        self.port = 0
        self._server: asyncio.Server | None = None
        self._writers: set[asyncio.StreamWriter] = set()

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle_client, "127.0.0.1", self.port)
        socket = self._server.sockets[0]
        self.port = int(socket.getsockname()[1])

    async def pause(self) -> None:
        if self._server is not None:
            self._server.close()
        writers = list(self._writers)
        self._writers.clear()
        for writer in writers:
            writer.close()
        if self._server is not None:
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._server.wait_closed(), timeout=1.0)
            self._server = None

    async def resume(self) -> None:
        await self.start()

    async def close(self) -> None:
        await self.pause()

    async def _handle_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        upstream_writer: asyncio.StreamWriter | None = None
        try:
            upstream_reader, upstream_writer = await asyncio.open_connection(
                self.target_host,
                self.target_port,
            )
            self._writers.update({writer, upstream_writer})
            with suppress(ConnectionError, OSError, asyncio.CancelledError):
                await asyncio.gather(
                    self._pipe(reader, upstream_writer),
                    self._pipe(upstream_reader, writer),
                )
        finally:
            for active_writer in (writer, upstream_writer):
                if active_writer is None:
                    continue
                self._writers.discard(active_writer)
                active_writer.close()
                with suppress(Exception):
                    await active_writer.wait_closed()

    @staticmethod
    async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()


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


def _proxy_dsn(dsn: str, port: int) -> str:
    parsed = urlsplit(_asyncpg_dsn(dsn))
    userinfo = ""
    if parsed.username is not None:
        userinfo = quote(unquote(parsed.username), safe="")
        if parsed.password is not None:
            userinfo += f":{quote(unquote(parsed.password), safe='')}"
        userinfo += "@"
    return urlunsplit(
        (
            parsed.scheme,
            f"{userinfo}127.0.0.1:{port}",
            parsed.path,
            parsed.query,
            parsed.fragment,
        )
    )


async def _wait_for_recovery(service: ResilientSessionService, timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while service.degraded and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.05)
    if service.degraded:
        raise RuntimeError("PostgreSQL session backend did not recover before timeout")


async def _validate_postgres_recovery(dsn: str) -> dict[str, Any]:
    import asyncpg

    parsed = urlsplit(_asyncpg_dsn(dsn))
    if parsed.hostname is None:
        raise ValueError("PostgreSQL DSN must include a host")
    proxy = _PostgresTcpProxy(parsed.hostname, parsed.port or 5432)
    await proxy.start()
    namespace = f"preprod_recovery_{uuid.uuid4().hex[:8]}"
    existing_session_id = f"sess-existing-{uuid.uuid4().hex[:8]}"
    new_session_id = f"sess-new-{uuid.uuid4().hex[:8]}"
    service = ResilientSessionService(
        PostgresSessionService(
            dsn=_proxy_dsn(dsn, proxy.port),
            namespace=namespace,
            connect_timeout=0.5,
        )
    )
    service._probe_interval_seconds = 0.2
    direct = await asyncpg.connect(_asyncpg_dsn(dsn))
    try:
        await service.create_session("recovery-e2e", "preprod-e2e", existing_session_id)
        await service.append_event(
            existing_session_id,
            SessionEvent(
                id="evt-before-outage",
                author="user",
                event_type="user_message",
                content={"role": "user", "parts": [{"text": "before outage"}]},
            ),
        )

        await proxy.pause()
        await asyncio.wait_for(
            service.append_event(
                existing_session_id,
                SessionEvent(
                    id="evt-during-outage",
                    author="recovery-e2e",
                    event_type="assistant_message",
                    content={"role": "assistant", "parts": [{"text": "during outage"}]},
                ),
            ),
            timeout=2.0,
        )
        if not service.degraded:
            raise RuntimeError(
                "PostgreSQL outage did not put the session backend into degraded mode"
            )

        await proxy.resume()
        await _wait_for_recovery(service)
        await service.append_event(
            existing_session_id,
            SessionEvent(
                id="evt-after-recovery",
                author="user",
                event_type="user_message",
                content={"role": "user", "parts": [{"text": "after recovery"}]},
            ),
        )
        await service.create_session("recovery-e2e", "preprod-e2e", new_session_id)
        await service.append_event(
            new_session_id,
            SessionEvent(
                id="evt-new-session",
                author="user",
                event_type="user_message",
                content={"role": "user", "parts": [{"text": "new session"}]},
            ),
        )

        rows = await direct.fetch(
            """
            SELECT session_id, id
            FROM ksadk_events
            WHERE namespace = $1 AND session_id = ANY($2::text[])
            ORDER BY session_id, seq_id
            """,
            namespace,
            [existing_session_id, new_session_id],
        )
        persisted = {(row["session_id"], row["id"]) for row in rows}
        expected = {
            (existing_session_id, "evt-before-outage"),
            (existing_session_id, "evt-after-recovery"),
            (new_session_id, "evt-new-session"),
        }
        if not expected.issubset(persisted):
            raise RuntimeError(f"PostgreSQL recovery persistence mismatch: {persisted!r}")
        return {
            "status": "pass",
            "recovered": not service.degraded,
            "new_session_persisted": (new_session_id, "evt-new-session") in persisted,
            "existing_session_resumed": (
                existing_session_id,
                "evt-after-recovery",
            )
            in persisted,
            "outage_event_persisted": (
                existing_session_id,
                "evt-during-outage",
            )
            in persisted,
        }
    finally:
        await service.aclose()
        await proxy.close()
        await direct.execute("DELETE FROM ksadk_states WHERE namespace = $1", namespace)
        await direct.execute("DELETE FROM ksadk_events WHERE namespace = $1", namespace)
        await direct.execute("DELETE FROM ksadk_sessions WHERE namespace = $1", namespace)
        await direct.close()


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
    recovery_result = await _validate_postgres_recovery(dsn) if dsn else {"status": "skipped"}
    return {
        "overall_status": "pass",
        "frameworks": framework_results,
        "postgres_readability": postgres_result,
        "postgres_recovery": recovery_result,
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

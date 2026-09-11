"""Bounded SQLite/ASGI benchmark; excludes model inference and network latency."""

import asyncio
import json
import platform
import statistics
import tempfile
import time
from pathlib import Path

import httpx
from fastapi import FastAPI

from ksadk.plugins.teams.api import create_router
from ksadk.plugins.teams.application import TeamsApplication
from ksadk.plugins.teams.contracts import Actor, GroupCreateInput, MessageInput, TaskCreateInput


async def main():
    owner = Actor("local-studio", "benchmark-owner")
    with tempfile.TemporaryDirectory(prefix="teams-benchmark-") as root:
        feature = TeamsApplication(
            path=Path(root) / "teams.sqlite",
            authority_ref="local-benchmark",
            host=object(),
            actor=lambda: owner,
            list_build_ids=lambda: [],
            workspace_root=Path(root) / "workspaces",
        )
        await feature.runtime.start(background=False)
        try:
            domain = feature.domain
            binding = {
                "bindingRef": "fixture",
                "providerRef": "controlled",
                "kind": "local_build",
                "capabilities": {
                    key: True
                    for key in ("enqueue", "leader", "cancel", "restore", "interaction", "steer")
                },
            }
            snapshot = domain.create_group(
                owner,
                GroupCreateInput(
                    name="性能基准",
                    members=[
                        {"memberId": f"member{i}", "name": f"Member {i}", "bindingRef": "fixture"}
                        for i in range(8)
                    ],
                    leaderMemberId="member0",
                    idempotencyKey="create",
                ),
                {"fixture": binding},
            )
            gid = snapshot["group"]["groupId"]
            started = domain.send(
                owner,
                gid,
                MessageInput(
                    parts=[{"kind": "text", "text": "性能基准"}],
                    intent="start_goal",
                    idempotencyKey="goal",
                ),
            )
            previous = None
            for i in range(200):
                task = domain.create_task(
                    owner,
                    gid,
                    started["teamRunId"],
                    TaskCreateInput(
                        title=f"Node {i}",
                        dependencies=[previous] if previous else [],
                    ),
                    f"task{i}",
                )
                previous = task["taskId"]
            app = FastAPI()
            app.include_router(create_router(feature), prefix="/api/v1")
            latencies = []
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://local"
            ) as http:
                for i in range(10_000):
                    tick = time.perf_counter()
                    response = await http.post(
                        f"/api/v1/groups/{gid}/messages",
                        json={
                            "parts": [{"kind": "text", "text": f"Evidence note {i}"}],
                            "intent": "note",
                            "idempotencyKey": f"note{i}",
                        },
                    )
                    response.raise_for_status()
                    latencies.append((time.perf_counter() - tick) * 1000)
                tick = time.perf_counter()
                response = await http.get(f"/api/v1/groups/{gid}")
                snapshot_ms = (time.perf_counter() - tick) * 1000
                final = response.json()
                assert len(final["members"]) == 8 and len(final["tasks"]) == 200
                assert len(final["messages"]) == 10_001
                assert len({m["messageId"] for m in final["messages"]}) == 10_001
                assert len(final["deliveries"]) == 1
                execution = domain.execution(owner, gid, started["teamRunId"])
                assert len(execution["nodes"]) == 200 and len(execution["edges"]) == 199
            result = {
                "environment": {
                    "os": platform.platform(),
                    "python": platform.python_version(),
                    "architecture": platform.machine(),
                },
                "transport": "in-process ASGI + actual SQLite WAL/FULL; no model/network",
                "members": 8,
                "taskNodes": 200,
                "notes": 10_000,
                "messageApiP50Ms": statistics.median(latencies),
                "messageApiP95Ms": sorted(latencies)[9499],
                "snapshotMs": snapshot_ms,
                "snapshotBytes": len(response.content),
                "watermark": final["watermark"],
                "duplicateOrMissingMessages": 0,
                "spuriousDeliveries": 0,
            }
            print(json.dumps(result, ensure_ascii=False, indent=2))
        finally:
            await feature.runtime.close()


if __name__ == "__main__":
    asyncio.run(main())

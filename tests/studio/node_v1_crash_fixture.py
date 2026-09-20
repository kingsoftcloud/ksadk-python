"""Subprocess-only test helper: SIGKILL after real Kernel inbox commit."""

import asyncio
import json
import os
import signal
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from ksadk.plugins.teams.cloud_contracts import parse_node_command
from tests.kernel.teams_host_harness import host_harness
from tests.studio.test_teams_node_v1 import make_node, prepare, process, submit


async def main(directory, phase):
    fixture = host_harness.__wrapped__(SimpleNamespace(param="sqlite"), directory)
    h = await anext(fixture)

    async def forbid_submit(command):
        raise AssertionError("a persisted inbox must never request a new native permit")

    node = await make_node(
        h, directory / "node", native_provider=forbid_submit if phase == "recover" else None
    )
    if phase == "kill":
        await process(node, h, prepare(h))
        await node.flush_outbox()
        original = h.host.submit_execution

        async def committed_then_kill(*args, **kwargs):
            await original(*args, **kwargs)
            os.kill(os.getpid(), signal.SIGKILL)

        h.host.submit_execution = committed_then_kill
        await process(node, h, submit(h))
        raise AssertionError("SIGKILL did not terminate the process")
    try:
        h.context = await h.registry.get("context-test", expected_incarnation=h.incarnation)
        h.preparation = replace(h.preparation, context=h.context)
        row = node.db.execute(
            "SELECT body FROM node_v1_operations WHERE state='invoking'"
        ).fetchone()
        command = parse_node_command(json.loads(row["body"]))
        # Server reissues transport authority over the unchanged command/digest.
        command = command.model_copy(
            update={
                "authorization": command.authorization.model_copy(
                    update={"permit": h.permit("enqueue").model_dump_json()}
                )
            }
        )
        result = await process(node, h, command)
        assert result.phase == "submitted", result
        message = await h.store.load_by_idempotency(
            h.context.ref.sessionId, h.context.ref.idempotencyKey
        )
        print(
            json.dumps(
                {
                    "status": result.receipt.status,
                    "sameCommand": str(message.command.command_id) == command.ref.commandId,
                    "inboxRows": len(
                        await h.store.list_messages(h.context.grant.agent_instance_id)
                    ),
                }
            )
        )
    finally:
        await node.close()
        await fixture.aclose()


if __name__ == "__main__":
    asyncio.run(main(Path(sys.argv[1]), sys.argv[2]))

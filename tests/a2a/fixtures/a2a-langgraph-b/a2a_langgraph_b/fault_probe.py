"""Opt-in acceptance fault at the wire boundary; never installed by the SDK."""
from contextlib import asynccontextmanager
import json
import logging


def install_response_loss_probe():
    from ksadk.a2a.space_client import A2ASpaceClient

    original = A2ASpaceClient.open_prepared_message_stream
    if getattr(original, "acceptance_response_loss_probe", False):
        return

    @asynccontextmanager
    async def open_stream(self, prepared, message):
        selected = False
        for part in message.parts:
            try:
                payload = json.loads(part.text)
            except (ValueError, TypeError):
                continue
            if isinstance(payload, dict) and payload.get("acceptance_drop_response") is True:
                selected = True
        async with original(self, prepared, message) as stream:
            if not selected:
                yield stream
                return
            iterator = aiter(stream)
            first = await anext(iterator)
            kind = first.WhichOneof("payload")
            task = getattr(first, kind) if kind else None
            logging.getLogger(__name__).warning(
                "A2A_ACCEPTANCE_RESPONSE_DROPPED platform_task=%s message=%s remote_task=%s",
                prepared.platform_task_id, message.message_id, getattr(task, "id", ""),
            )

            async def lost_response():
                raise ConnectionError("acceptance probe dropped the received first response")
                yield  # pragma: no cover - async iterator contract

            yield lost_response()

    open_stream.acceptance_response_loss_probe = True
    A2ASpaceClient.open_prepared_message_stream = open_stream

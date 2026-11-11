"""Held model boundaries for deterministic Native control tests."""

import asyncio
from threading import Event

from ksadk.studio.contracts import Usage
from ksadk.studio.model_client import ModelResponse, ModelStreamChunk


class ControlledGateway:
    def __init__(self):
        self.release = Event()
        self.requests = []

    async def complete(self, model, **kwargs):
        messages = str(kwargs["messages"])
        self.requests.append(messages)
        if "HOLD_FOR_CANCEL" in messages and "ACK_NEXT_TURN" not in messages:
            while not self.release.is_set():
                await asyncio.sleep(0.01)
            text = "STALE_CANCEL_ANSWER"
        elif "ACK_NEXT_TURN" in messages:
            text = "ACK_NEXT_TURN"
        elif "ACK_CORRECTION" in messages:
            text = "ACK_CORRECTION"
        else:
            while not self.release.is_set():
                await asyncio.sleep(0.01)
            text = "STALE_INITIAL_ANSWER"
        return ModelResponse(
            content=text,
            finish_reason="stop",
            tool_calls=[],
            raw_message={},
            usage=Usage(input_tokens=30, output_tokens=5, total_tokens=35, reported=True),
        )

    async def stream(self, model, **kwargs):
        response = await self.complete(model, **kwargs)
        yield ModelStreamChunk(
            text=response.content, usage=response.usage, finish_reason="stop", done=True
        )

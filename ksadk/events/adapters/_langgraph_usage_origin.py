"""Keep model usage provenance when a graph copies or serializes the result."""

from uuid import UUID, uuid4

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import AIMessage, AIMessageChunk
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, LLMResult

from ksadk.events.canonical import UsageReported

_ORIGIN_KEY = "ksadk_usage_origin"


def message_usage_origin(message: AIMessage) -> str | None:
    origin = message.response_metadata.get(_ORIGIN_KEY)
    if isinstance(origin, str):  # Checkpoints written before chunk stamping.
        return origin or None
    if isinstance(origin, dict) and len(origin) == 1:
        key, present = next(iter(origin.items()))
        if isinstance(key, str) and key and present is True:
            return key
    return None


def event_usage_origin(event: UsageReported) -> str | None:
    origin = event.source.metadata.get("usage_origin")
    return origin if event.source.framework == "langgraph" and isinstance(origin, str) else None


def checkpoint_usage_origins(snapshot) -> set[str]:
    """Read provenance from messages already present before the continuation."""
    values = getattr(snapshot, "values", None)
    messages = values.get("messages") if isinstance(values, dict) else None
    origins: set[str] = set()
    if isinstance(messages, (list, tuple)):
        origins.update(
            origin for message in messages
            if isinstance(message, AIMessage) and (origin := message_usage_origin(message))
        )
    for task in getattr(snapshot, "tasks", ()) or ():
        origins.update(checkpoint_usage_origins(getattr(task, "state", None)))
    return origins


class NativeUsageOrigin(BaseCallbackHandler):
    """Tie returned messages to the model callback that produced their usage."""

    run_inline = True

    def __init__(self) -> None:
        self._invocation_id = uuid4().hex

    def origin_for_run(self, run_id: UUID | str) -> str:
        return f"{self._invocation_id}:{run_id}"

    def _metadata(self, message: AIMessage, run_id: UUID) -> dict:
        # LangChain concatenates strings during chunk addition. A map of
        # origin -> True merges idempotently and survives JSON/checkpoint copies.
        return {
            **message.response_metadata,
            _ORIGIN_KEY: {self.origin_for_run(run_id): True},
        }

    def on_llm_new_token(
        self, token: str, *, chunk=None, run_id: UUID, **kwargs
    ) -> None:
        if isinstance(chunk, ChatGenerationChunk) and isinstance(chunk.message, AIMessageChunk):
            # stream()/astream() yield this message after invoking the callback.
            # Stamp the actual chunk (also the usage-free chunks), before callers
            # can merge or copy it. The synthetic last chunk is yielded directly.
            chunk.message.response_metadata = self._metadata(chunk.message, run_id)

    def on_llm_end(self, response: LLMResult, *, run_id: UUID, **kwargs) -> None:
        for generations in response.generations:
            for generation in generations:
                if not isinstance(generation, ChatGeneration):
                    continue
                message = generation.message
                if isinstance(message, AIMessage) and (
                    message.usage_metadata or message.response_metadata.get("token_usage")
                ):
                    # invoke()/ainvoke() read the generation after the callback.
                    # Copy the result so a provider's reusable response stays unchanged.
                    generation.message = message.model_copy(
                        update={"response_metadata": self._metadata(message, run_id)}
                    )

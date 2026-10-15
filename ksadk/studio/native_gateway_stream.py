"""Validate canonical Gateway facts before projecting a Native model turn."""

from collections.abc import Mapping

from ksadk._model_input import ProviderInputObservation
from ksadk.harness.native.model import ModelUsage
from ksadk.model_gateway.events import ModelStreamEvent, ModelStreamEventType
from ksadk.studio.errors import StudioError


def _invalid() -> StudioError:
    # Provider text, model identities and raw malformed fields are not public
    # diagnostics. Reuse the existing non-retryable host protocol rejection.
    return StudioError(
        "MODEL_RESPONSE_INVALID", "Canonical model stream contract mismatch", status_code=422
    )


def validate_gateway_event(event: ModelStreamEvent, *, model: str, sequence: int) -> None:
    """The consumer must not hide foreign or lost events by renumbering them."""
    if (
        not isinstance(event, ModelStreamEvent)
        or not isinstance(event.type, ModelStreamEventType)
        or type(event.model) is not str
        or event.model != model
        or type(event.sequence) is not int
        or event.sequence != sequence
        or not isinstance(event.metadata, Mapping)
    ):
        raise _invalid()
    if event.type in {ModelStreamEventType.TEXT_DELTA, ModelStreamEventType.REASONING_DELTA,
                       ModelStreamEventType.PUBLIC_SUMMARY_DELTA}:
        if type(event.text) is not str:
            raise _invalid()
    if (event.type is ModelStreamEventType.INPUT_TRANSFORMATIONS) != (
        event.provider_input is not None
    ) or (event.provider_input is not None
          and not isinstance(event.provider_input, ProviderInputObservation)):
        raise _invalid()
    if event.provider_content is not None and (
        event.type is not ModelStreamEventType.TURN_COMPLETED
        or not isinstance(event.provider_content, tuple)
        or any(not isinstance(block, Mapping) for block in event.provider_content)
    ):
        raise _invalid()


def gateway_stream_usage(event: ModelStreamEvent) -> ModelUsage:
    """A reported update requires both primary counters, without coercion.

    Optional cache/reasoning subdivisions may be absent. No update means
    unknown usage; an explicit pair of integer zeroes means reported zero.
    """
    value = event.usage
    if (
        not isinstance(value, Mapping)
        or "input_tokens" not in value
        or "output_tokens" not in value
    ):
        raise _invalid()
    try:
        return ModelUsage(
            input_tokens=value["input_tokens"],
            output_tokens=value["output_tokens"],
            cached_input_tokens=value.get("cached_tokens", 0),
            reasoning_tokens=value.get("reasoning_tokens", 0),
        )
    except (ValueError, TypeError):
        raise _invalid() from None

"""Record input observations without modifying model history or execution rights."""

from ksadk._model_input import transformation_summary

from .model import ModelInputReceipt, ModelProfile
from .state import RunSnapshot


def input_binding_failure(profile: ModelProfile, receipt: ModelInputReceipt | None) -> str | None:
    if receipt is not None and receipt.profile_digest != profile.digest:
        return "model_input_profile_mismatch"
    return None


def observe_input(snapshot: RunSnapshot, receipt: ModelInputReceipt | None) -> RunSnapshot:
    # A missing report does not turn the preceding observation into reported [].
    if receipt is None:
        return snapshot
    return snapshot.model_copy(update={"model_input_observation": receipt})


def input_event(receipt: ModelInputReceipt) -> tuple[str, dict]:
    return "model.input_observed", {
        "request_id": receipt.request_id,
        "attempt_id": receipt.attempt_id,
        "receipt_digest": receipt.digest,
        "provider_prefix_digest": receipt.provider_prefix_digest,
        "source": receipt.source,
        **transformation_summary(receipt.transformations_json),
    }

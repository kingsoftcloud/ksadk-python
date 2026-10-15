"""Pure generation re-admission; pending effects retain their original identity."""

from .activation_contract import ActivationResourceRef
from .state import RunSnapshot
from .transitions import commit_transition


def recovery_transition(snapshot: RunSnapshot, current: ActivationResourceRef | None):
    previous = (ActivationResourceRef.model_validate(snapshot.activation_resource_ref)
                if snapshot.activation_resource_ref is not None else None)
    if current is None:
        if previous is not None:
            raise ValueError("activation resource binding unavailable")
        return None
    if current.scope != snapshot.scope or (
        previous is not None and previous.immutable_identity != current.immutable_identity
    ):
        raise ValueError("activation resource identity mismatch")
    if previous == current:
        return None
    updated = snapshot.model_copy(update={
        "activation_resource_ref": current.model_dump(mode="json"),
    })
    return commit_transition(snapshot, updated, "activation.recovered", {
        "status": "admitted" if previous is None else "rebound",
        "generation": current.generation,
        "previous_generation": previous.generation if previous is not None else None,
        "mcp_leases": [lease.model_dump(mode="json") for lease in current.mcp_leases],
    })

"""Deployment-owned Agent aliases for historical Kernel session owners."""

import os


def session_agent_ids(agent_id: str) -> tuple[str, ...]:
    """Accept only this deployment's public Agent and internal instance pair.

    The aliases come from platform-injected environment, never request metadata.
    Business identity authorization remains required for every selected session.
    """
    public_id = os.environ.get("AGENT_RUNTIME_ID", "").strip()
    instance_id = os.environ.get("AGENT_INSTANCE_ID", "").strip()
    if public_id and instance_id and agent_id == public_id and instance_id != public_id:
        return (public_id, instance_id)
    return (agent_id,)

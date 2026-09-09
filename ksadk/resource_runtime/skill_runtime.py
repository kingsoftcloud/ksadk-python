"""Instantiate the sandbox target frozen in a resource Build after host admission."""

from pathlib import Path

from pydantic import SecretStr

from ksadk.resource_runtime.snapshots import ConnectionTarget, ResourceSnapshot
from ksadk.sandbox.e2b_connection import ExplicitE2BConnection
from ksadk.skills.runtime.backends.e2b import E2BSkillRuntimeBackend


def create_bound_skill_runtime(
    snapshot: ResourceSnapshot,
    binding_id: str,
    *,
    current_connection: ConnectionTarget,
    api_key: SecretStr,
    artifact_directory: Path | None = None,
) -> E2BSkillRuntimeBackend:
    """Host revalidates account/grant first; only secret values may rotate here.

    This factory does not read process env, create a sandbox, or authorize tool
    execution. The target and network policy are immutable Build inputs.
    """
    snapshot = ResourceSnapshot.model_validate(snapshot.model_dump())
    target = snapshot.binding(binding_id).skill_execution
    if target is None:
        raise ValueError("SKILL_EXECUTION_TARGET_REQUIRED")
    if target.connection != current_connection:
        raise ValueError("RESOURCE_CONNECTION_CHANGED")
    connection = ExplicitE2BConnection(
        api_url=target.connection.endpoint,
        domain=target.domain,
        api_key=api_key,
    )
    return E2BSkillRuntimeBackend(
        template_id=target.template_id,
        timeout=target.timeout,
        allow_internet_access=target.allow_internet_access,
        connection=connection,
        artifact_directory=artifact_directory,
    )

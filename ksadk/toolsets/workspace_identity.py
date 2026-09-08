"""Identity-scoped root resolution for built-in workspace tools."""

from pathlib import Path

from ksadk.runtime_context import get_current_identity_context
from ksadk.sessions.invocation_identity import identity_scope_ref


def identity_workspace_root(root: Path) -> Path:
    """Append the verified identity scope while preserving caller-owned roots."""

    identity = get_current_identity_context()
    if identity.is_empty:
        return root
    return root / "identities" / identity_scope_ref(identity)

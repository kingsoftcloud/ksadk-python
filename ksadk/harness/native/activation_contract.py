"""Nonsecret resource identity; generations do not alter immutable Run config."""

from pydantic import Field

from .state import Contract, RunScope


class McpLeaseRef(Contract):
    name: str = Field(min_length=1, max_length=128)
    generation: str = Field(min_length=1, max_length=128)
    descriptor_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class ActivationResourceRef(Contract):
    scope: RunScope
    activation_key: str = Field(min_length=1, max_length=512)
    generation: str = Field(min_length=1, max_length=128)
    profile_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    plugin_lock_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    mcp_leases: tuple[McpLeaseRef, ...] = Field(default=(), max_length=32)

    @property
    def immutable_identity(self):
        return (self.scope, self.activation_key, self.profile_digest, self.plugin_lock_digest,
                tuple((lease.name, lease.descriptor_digest) for lease in self.mcp_leases))

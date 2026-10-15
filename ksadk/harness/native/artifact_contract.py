"""Pure immutable artifact identity, shared by inputs and effect ports."""

from pydantic import Field

from .state import Contract


class ArtifactRef(Contract):
    """Opaque host-owned content reference; tool text cannot create one."""

    uri: str = Field(min_length=1, max_length=2048, pattern=r"^artifact://\S+$")
    content_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    size_bytes: int = Field(strict=True, ge=0)
    mime_type: str = Field(min_length=1, max_length=160, pattern=r"^[^\r\n]+$")

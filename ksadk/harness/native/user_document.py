"""Host-derived document data owned by the original user turn."""

import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .artifact_contract import ArtifactRef


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


class NativeUserDocument(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", revalidate_instances="always")
    version: Literal["native-user-document/v1"] = "native-user-document/v1"
    name: str = Field(strict=True, min_length=1, max_length=256)
    media_type: str = Field(strict=True, min_length=1, max_length=256)
    source_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_size_bytes: int = Field(strict=True, ge=1)
    text: str = Field(strict=True, min_length=1, max_length=32768)
    extraction_method: str = Field(strict=True, min_length=1, max_length=64)
    truncated: bool = Field(default=False, strict=True)
    extracted_text_chars: int | None = Field(default=None, strict=True, ge=0)
    total_pages: int | None = Field(default=None, strict=True, ge=1)
    extracted_pages: int | None = Field(default=None, strict=True, ge=0)
    text_extraction_complete: bool | None = Field(default=None, strict=True)
    failed_page_count: int | None = Field(default=None, strict=True, ge=0)
    pages_without_text: int | None = Field(default=None, strict=True, ge=0)
    # Transient admission data; only the reference and preview enter messages.
    full_text: str | None = Field(default=None, strict=True, max_length=20_000_000, repr=False)
    text_artifact: ArtifactRef | None = None

    def descriptor(self):
        metadata = self.model_dump(mode="json", exclude={"text", "full_text"}, exclude_none=True)
        return {
            **metadata,
            "source_ref": "attachment://sha256/" + self.source_digest,
            "text_digest": hashlib.sha256(self.text.encode()).hexdigest(),
        }

    def message(self, owner):
        metadata = self.descriptor()
        return {
            "role": "user",
            "kind": "user_document_observation",
            "document_owner_digest": document_owner_digest(owner),
            "document_source": metadata,
            "content": _json(
                {
                    "type": "ksadk.user-document/v1",
                    "trust": "untrusted",
                    "source": metadata,
                    "text": self.text,
                }
            ),
        }


def document_owner_digest(message):
    # Historical projection adds provenance fields; those do not change the
    # original user body or the document sources sealed in the same start.
    body = {key: message.get(key) for key in ("role", "content", "user_document_sources")}
    return hashlib.sha256(_json(body).encode()).hexdigest()


def user_document_indexes(messages, goal_index):
    """Bind all document observations to their contiguous canonical user owner."""
    owner = messages[goal_index]
    sources = owner.get("user_document_sources") or ()
    indexes = []
    for offset, source in enumerate(sources, 1):
        index = goal_index + offset
        if index >= len(messages):
            raise ValueError("native_user_document_source_mismatch")
        message = messages[index]
        if (
            message.get("role") != "user"
            or message.get("kind") != "user_document_observation"
            or message.get("document_owner_digest") != document_owner_digest(owner)
            or message.get("document_source") != source
        ):
            raise ValueError("native_user_document_source_mismatch")
        try:
            body = json.loads(message["content"])
            if (
                body["type"] != "ksadk.user-document/v1"
                or body["trust"] != "untrusted"
                or body["source"] != source
                or hashlib.sha256(body["text"].encode()).hexdigest() != source["text_digest"]
            ):
                raise ValueError
        except (KeyError, TypeError, AttributeError, ValueError):
            raise ValueError("native_user_document_source_mismatch") from None
        indexes.append(index)
    return tuple(indexes)

from __future__ import annotations

import pytest

from ksadk.studio.attachment_store import ConversationAttachmentStore
from ksadk.studio.errors import StudioError
from ksadk.studio.workspace import Workspace


def test_content_addressed_attachment_keeps_authoritative_metadata(tmp_path) -> None:
    store = ConversationAttachmentStore(Workspace(tmp_path))

    first = store.store(b"same content", filename="notes.md", media_type="text/markdown")
    second = store.store(
        b"same content",
        filename="renamed.json",
        media_type="application/json",
    )

    assert second == first
    resolved = store.resolve(str(first["attachmentRef"]))
    assert resolved.name == "notes.md"
    assert resolved.media_type == "text/markdown"


def test_content_addressed_attachment_detects_same_size_tampering(tmp_path) -> None:
    store = ConversationAttachmentStore(Workspace(tmp_path))
    stored = store.store(b"trusted", filename="notes.txt", media_type="text/plain")
    resolved = store.resolve(str(stored["attachmentRef"]))
    resolved.path.write_bytes(b"altered")

    with pytest.raises(StudioError) as captured:
        store.resolve(str(stored["attachmentRef"]))

    assert captured.value.code == "CONVERSATION_ATTACHMENT_CORRUPT"

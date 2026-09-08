"""Bounded, content-addressed local attachments for ConversationInput."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

from ksadk.studio.errors import StudioError, not_found
from ksadk.studio.workspace import Workspace

_ATTACHMENT_REF = re.compile(r"^attachment://sha256/(?P<digest>[0-9a-f]{64})$")
_SAFE_FILENAME = re.compile(r"[^A-Za-z0-9._-]+")
_TEXT_MEDIA_TYPES = {
    "application/json",
    "application/toml",
    "application/xml",
    "application/x-httpd-php",
    "application/x-sh",
    "application/x-yaml",
}
_TEXT_SUFFIXES = {
    ".csv",
    ".css",
    ".go",
    ".html",
    ".ini",
    ".java",
    ".js",
    ".json",
    ".jsx",
    ".log",
    ".md",
    ".py",
    ".rs",
    ".sh",
    ".toml",
    ".ts",
    ".tsx",
    ".txt",
    ".xml",
    ".yaml",
    ".yml",
}


@dataclass(frozen=True)
class StoredConversationAttachment:
    attachment_ref: str
    path: Path
    name: str
    media_type: str
    size: int
    digest: str


class ConversationAttachmentStore:
    """Persist only the small image/text inputs supported by local Studio."""

    MAX_BYTES = 1_500_000

    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace
        self.root = self.workspace.resolve(".agentkit/assets/conversation-attachments")
        self.root.mkdir(parents=True, exist_ok=True)

    def store(self, content: bytes, *, filename: str, media_type: str) -> dict[str, object]:
        if not content:
            raise StudioError(
                "CONVERSATION_ATTACHMENT_EMPTY",
                "会话附件不能为空",
                status_code=422,
                field="file",
            )
        if len(content) > self.MAX_BYTES:
            raise StudioError(
                "CONVERSATION_ATTACHMENT_TOO_LARGE",
                "会话附件不能超过 1.5 MiB",
                status_code=413,
                field="file",
                details={"maxBytes": self.MAX_BYTES},
            )
        normalized_type = media_type.split(";", 1)[0].strip().lower()
        safe_name = _SAFE_FILENAME.sub("-", Path(filename).name).strip(".-")[:160]
        safe_name = safe_name or "attachment"
        if not self._supported(content, filename=safe_name, media_type=normalized_type):
            raise StudioError(
                "CONVERSATION_ATTACHMENT_TYPE_UNSUPPORTED",
                "会话附件仅支持图片或 UTF-8 文本、代码文件",
                status_code=415,
                field="file",
            )

        digest = hashlib.sha256(content).hexdigest()
        content_path = self.root / f"{digest}.bin"
        metadata_path = self.root / f"{digest}.json"
        if not content_path.is_file():
            self.workspace.atomic_write_bytes(content_path, content)
        if not metadata_path.is_file():
            self.workspace.atomic_write_text(
                metadata_path,
                json.dumps(
                    {
                        "digest": digest,
                        "name": safe_name,
                        "mediaType": normalized_type or "application/octet-stream",
                        "size": len(content),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n",
            )
        # The reference identifies content, so its persisted metadata is the
        # authority as well.  Returning a second upload's conflicting name or
        # media type would create a ref that immediately fails on resolution.
        stored = self.resolve(f"attachment://sha256/{digest}")
        return {
            "attachmentRef": stored.attachment_ref,
            "name": stored.name,
            "mediaType": stored.media_type,
            "size": stored.size,
            "digest": stored.digest,
        }

    def resolve(self, attachment_ref: str) -> StoredConversationAttachment:
        match = _ATTACHMENT_REF.fullmatch(attachment_ref)
        if match is None:
            raise not_found("conversation-attachment", attachment_ref)
        digest = match.group("digest")
        content_path = self.root / f"{digest}.bin"
        metadata_path = self.root / f"{digest}.json"
        if not content_path.is_file() or not metadata_path.is_file():
            raise not_found("conversation-attachment", attachment_ref)
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            content = content_path.read_bytes()
            content_size = len(content)
        except (OSError, json.JSONDecodeError) as exc:
            raise StudioError(
                "CONVERSATION_ATTACHMENT_CORRUPT",
                "会话附件元数据损坏",
                status_code=409,
                details={"attachmentRef": attachment_ref},
            ) from exc
        actual_digest = hashlib.sha256(content).hexdigest()
        if (
            metadata.get("digest") != digest
            or actual_digest != digest
            or int(metadata.get("size") or -1) != content_size
        ):
            raise StudioError(
                "CONVERSATION_ATTACHMENT_CORRUPT",
                "会话附件完整性校验失败",
                status_code=409,
                details={"attachmentRef": attachment_ref},
            )
        return StoredConversationAttachment(
            attachment_ref=attachment_ref,
            path=content_path,
            name=str(metadata.get("name") or "attachment"),
            media_type=str(metadata.get("mediaType") or "application/octet-stream"),
            size=content_size,
            digest=digest,
        )

    @staticmethod
    def _supported(content: bytes, *, filename: str, media_type: str) -> bool:
        if media_type.startswith("image/"):
            return True
        if (
            media_type.startswith("text/")
            or media_type in _TEXT_MEDIA_TYPES
            or Path(filename).suffix.lower() in _TEXT_SUFFIXES
        ):
            try:
                content.decode("utf-8")
            except UnicodeDecodeError:
                return False
            return True
        return False


__all__ = ["ConversationAttachmentStore", "StoredConversationAttachment"]

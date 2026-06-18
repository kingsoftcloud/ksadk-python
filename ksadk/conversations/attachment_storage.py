from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from ksadk.common.constants import get_ks3_endpoints
from ksadk.sessions.local_service import resolve_local_session_dir

logger = logging.getLogger(__name__)

UPLOAD_URI_SCHEME = "ksadk-upload://"
HOSTED_UPLOAD_URI_SCHEME = "ae-upload://"
UPLOAD_METADATA_SUFFIX = ".meta.json"


@dataclass(frozen=True)
class AttachmentBytes:
    data: bytes
    display_name: str
    mime_type: str
    local_path: Optional[Path] = None


def uploads_dir() -> Path:
    path = resolve_local_session_dir() / "files"
    path.mkdir(parents=True, exist_ok=True)
    return path


def sanitize_name(raw_name: str | None, *, fallback: str) -> str:
    candidate = Path(str(raw_name or "").strip()).name
    return candidate or fallback


def guess_mime_type(display_name: str) -> str:
    guessed, _ = mimetypes.guess_type(display_name)
    return guessed or "application/octet-stream"


def parse_file_id(file_uri: str) -> str:
    normalized = str(file_uri or "").strip()
    if normalized.startswith(UPLOAD_URI_SCHEME):
        return normalized.removeprefix(UPLOAD_URI_SCHEME).strip("/")
    if normalized.startswith(HOSTED_UPLOAD_URI_SCHEME):
        return normalized.removeprefix(HOSTED_UPLOAD_URI_SCHEME).strip("/")
    return ""


def is_runtime_upload_uri(file_uri: str) -> bool:
    return str(file_uri or "").strip().startswith(UPLOAD_URI_SCHEME)


def is_hosted_upload_uri(file_uri: str) -> bool:
    return str(file_uri or "").strip().startswith(HOSTED_UPLOAD_URI_SCHEME)


def _content_type_without_params(value: str | None) -> str:
    return str(value or "").split(";", 1)[0].strip() or "application/octet-stream"


def _download_hosted_attachment(file_uri: str) -> AttachmentBytes | None:
    if not is_hosted_upload_uri(file_uri):
        return None
    try:
        from ksadk.api.client import AgentEngineClient

        content = AgentEngineClient().download_attachment_content(file_uri)
    except Exception as exc:
        logger.warning("Hosted attachment download failed: %s", exc)
        return None
    file_id = parse_file_id(file_uri)
    display_name = sanitize_name(content.display_name, fallback=file_id or "uploaded_file")
    return AttachmentBytes(
        data=content.data,
        display_name=display_name,
        mime_type=(
            _content_type_without_params(content.content_type)
            or guess_mime_type(display_name)
        ),
    )


def default_bucket_name() -> str:
    explicit = os.getenv("KS3_BUCKET", "").strip()
    if explicit:
        return explicit
    account_id = os.getenv("KSYUN_ACCOUNT_ID", "").strip()
    region = os.getenv("KS3_REGION") or os.getenv("KSYUN_REGION") or "cn-beijing-6"
    if account_id:
        return f"agentengine-{account_id}-{region}"
    return ""


def _region() -> str:
    return os.getenv("KS3_REGION") or os.getenv("KSYUN_REGION") or "cn-beijing-6"


class AttachmentStorageService:
    def __init__(self, *, root_dir: Path | None = None):
        self.root_dir = root_dir or uploads_dir()

    async def store(
        self,
        *,
        data: bytes,
        file_id: str,
        display_name: str | None,
        mime_type: str | None,
    ) -> tuple[str, Path]:
        return await asyncio.to_thread(
            self.store_sync,
            data=data,
            file_id=file_id,
            display_name=display_name,
            mime_type=mime_type,
        )

    def store_sync(
        self,
        *,
        data: bytes,
        file_id: str,
        display_name: str | None,
        mime_type: str | None,
    ) -> tuple[str, Path]:
        self.root_dir.mkdir(parents=True, exist_ok=True)
        safe_name = sanitize_name(display_name, fallback=file_id or "uploaded_file")
        resolved_mime = str(mime_type or "").strip() or guess_mime_type(safe_name)
        local_path = self._local_path(file_id=file_id, display_name=safe_name)
        local_path.write_bytes(data)

        bucket = default_bucket_name()
        object_key = self._object_key(file_id=file_id, display_name=safe_name)
        metadata = {
            "file_id": file_id,
            "backend": "local",
            "bucket": bucket,
            "object_key": object_key,
            "region": _region(),
            "local_path": str(local_path),
            "display_name": safe_name,
            "mime_type": resolved_mime,
            "size_bytes": len(data),
            "fallback_reason": "",
        }
        try:
            self._run_async_sync(
                self._put_ks3_object(
                    bucket=bucket,
                    object_key=object_key,
                    data=data,
                    mime_type=resolved_mime,
                )
            )
            metadata["backend"] = "ks3"
        except Exception as exc:
            logger.warning("KS3 attachment upload failed, using local fallback: %s", exc)
            metadata["fallback_reason"] = str(exc)

        self._write_metadata(file_id, metadata)
        return f"{UPLOAD_URI_SCHEME}{file_id}", local_path

    def read(self, file_uri: str) -> AttachmentBytes | None:
        file_id = parse_file_id(file_uri)
        if not file_id:
            return None
        if is_hosted_upload_uri(file_uri):
            hosted = _download_hosted_attachment(file_uri)
            if hosted is None:
                return None
            metadata = {
                "file_id": file_id,
                "backend": "hosted",
                "display_name": hosted.display_name,
                "mime_type": hosted.mime_type,
                "size_bytes": len(hosted.data),
                "local_path": str(
                    self._local_path(file_id=file_id, display_name=hosted.display_name)
                ),
            }
            local_path = self._restore_local_cache(file_id, metadata, hosted.data)
            self._write_metadata(file_id, metadata | {"local_path": str(local_path)})
            return AttachmentBytes(
                data=hosted.data,
                display_name=hosted.display_name,
                mime_type=hosted.mime_type,
                local_path=local_path,
            )
        metadata = self._read_metadata(file_id)
        if metadata:
            if metadata.get("backend") == "ks3":
                try:
                    raw = self._run_async_sync(
                        self._read_ks3_object(
                            bucket=str(metadata.get("bucket") or ""),
                            object_key=str(metadata.get("object_key") or ""),
                        )
                    )
                    return AttachmentBytes(
                        data=raw,
                        display_name=str(metadata.get("display_name") or file_id),
                        mime_type=str(metadata.get("mime_type") or guess_mime_type(file_id)),
                        local_path=self._restore_local_cache(file_id, metadata, raw),
                    )
                except Exception as exc:
                    logger.warning("KS3 attachment read failed, trying local cache: %s", exc)

            local_path = Path(str(metadata.get("local_path") or ""))
            if local_path.is_file():
                try:
                    raw = local_path.read_bytes()
                except OSError:
                    raw = None
                if raw is not None:
                    return AttachmentBytes(
                        data=raw,
                        display_name=str(metadata.get("display_name") or local_path.name),
                        mime_type=str(metadata.get("mime_type") or guess_mime_type(local_path.name)),
                        local_path=local_path,
                    )

        legacy = self.resolve_legacy_local_path(file_id)
        if legacy is None:
            return None
        try:
            raw = legacy.read_bytes()
        except OSError:
            return None
        return AttachmentBytes(
            data=raw,
            display_name=legacy.name,
            mime_type=guess_mime_type(legacy.name),
            local_path=legacy,
        )

    def ensure_local_path(self, file_uri: str) -> Path | None:
        loaded = self.read(file_uri)
        if loaded is None:
            return None
        if loaded.local_path and loaded.local_path.is_file():
            return loaded.local_path
        file_id = parse_file_id(file_uri)
        metadata = self._read_metadata(file_id)
        return self._restore_local_cache(file_id, metadata, loaded.data) if metadata else None

    def resolve_legacy_local_path(self, file_id: str) -> Path | None:
        direct = self.root_dir / file_id
        if direct.is_file():
            return direct.resolve()
        for candidate in sorted(self.root_dir.glob(f"{file_id}*")):
            if candidate.is_file() and not candidate.name.endswith(UPLOAD_METADATA_SUFFIX):
                return candidate.resolve()
        return None

    async def _put_ks3_object(
        self,
        *,
        bucket: str,
        object_key: str,
        data: bytes,
        mime_type: str,
    ) -> None:
        await asyncio.to_thread(self._put_ks3_object_sync, bucket, object_key, data, mime_type)

    async def _read_ks3_object(self, *, bucket: str, object_key: str) -> bytes:
        return await asyncio.to_thread(self._read_ks3_object_sync, bucket, object_key)

    def _put_ks3_object_sync(self, bucket: str, object_key: str, data: bytes, mime_type: str) -> None:
        if not bucket:
            raise ValueError("KS3 bucket is not available")
        from ks3.connection import Connection

        ak = os.getenv("KSYUN_ACCESS_KEY") or os.getenv("KS3_ACCESS_KEY")
        sk = os.getenv("KSYUN_SECRET_KEY") or os.getenv("KS3_SECRET_KEY")
        if not ak or not sk:
            raise ValueError("KS3 credentials are not configured")
        conn = Connection(ak, sk, host=self._endpoint())
        ks3_bucket = self._ensure_bucket(conn, bucket)
        key = ks3_bucket.new_key(object_key)
        key.set_contents_from_string(
            data,
            headers={"Content-Type": mime_type} if mime_type else None,
        )

    def _read_ks3_object_sync(self, bucket: str, object_key: str) -> bytes:
        if not bucket or not object_key:
            raise FileNotFoundError(object_key)
        from ks3.connection import Connection

        ak = os.getenv("KSYUN_ACCESS_KEY") or os.getenv("KS3_ACCESS_KEY")
        sk = os.getenv("KSYUN_SECRET_KEY") or os.getenv("KS3_SECRET_KEY")
        if not ak or not sk:
            raise ValueError("KS3 credentials are not configured")
        conn = Connection(ak, sk, host=self._endpoint())
        key = conn.get_bucket(bucket).get_key(object_key)
        if key is None:
            raise FileNotFoundError(object_key)
        return key.get_contents_as_string()

    @staticmethod
    def _ensure_bucket(conn: Any, bucket_name: str):
        try:
            bucket = conn.get_bucket(bucket_name)
            list(bucket.list(max_keys=1))
            return bucket
        except Exception as exc:
            text = str(exc)
            if "NoSuchBucket" not in text and "404" not in text:
                raise
        return conn.create_bucket(bucket_name)

    @staticmethod
    def _endpoint() -> str:
        _public, internal = get_ks3_endpoints(_region())
        return internal

    def _metadata_path(self, file_id: str) -> Path:
        return self.root_dir / f"{file_id}{UPLOAD_METADATA_SUFFIX}"

    def _write_metadata(self, file_id: str, metadata: dict[str, Any]) -> None:
        self._metadata_path(file_id).write_text(
            json.dumps(metadata, ensure_ascii=False),
            encoding="utf-8",
        )

    def _read_metadata(self, file_id: str) -> dict[str, Any]:
        if not file_id:
            return {}
        metadata_path = self._metadata_path(file_id)
        if not metadata_path.is_file():
            return {}
        try:
            payload = json.loads(metadata_path.read_text(encoding="utf-8"))
        except Exception:
            return {}
        return payload if isinstance(payload, dict) else {}

    def _local_path(self, *, file_id: str, display_name: str) -> Path:
        suffix = Path(display_name).suffix
        local_name = file_id if suffix and file_id.endswith(suffix) else f"{file_id}{suffix}"
        return self.root_dir / local_name

    def _restore_local_cache(self, file_id: str, metadata: dict[str, Any], data: bytes) -> Path:
        local_path = Path(str(metadata.get("local_path") or ""))
        if not local_path:
            local_path = self._local_path(
                file_id=file_id,
                display_name=str(metadata.get("display_name") or file_id),
            )
        local_path.parent.mkdir(parents=True, exist_ok=True)
        local_path.write_bytes(data)
        return local_path

    @staticmethod
    def _object_key(*, file_id: str, display_name: str) -> str:
        suffix = Path(display_name).suffix
        stored_name = file_id if suffix and file_id.endswith(suffix) else f"{file_id}{suffix}"
        day = datetime.utcnow().strftime("%Y/%m/%d")
        return f"agents/_runtime/attachments/{day}/{stored_name}"

    @staticmethod
    def _run_async_sync(coro):
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(coro)
        if loop.is_running():
            import concurrent.futures

            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                return executor.submit(lambda: asyncio.run(coro)).result()
        return loop.run_until_complete(coro)

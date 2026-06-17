from __future__ import annotations

import importlib

import httpx
import pytest

from ksadk.conversations.attachment_storage import AttachmentStorageService
from ksadk.conversations.attachments import resolve_attachment_storage_path


@pytest.mark.asyncio
async def test_runtime_upload_file_uses_ks3_metadata_and_attachment_content_reads_ks3(
    monkeypatch,
    tmp_path,
):
    server_app_module = importlib.import_module("ksadk.server.app")
    ui_dir = tmp_path / ".agentengine" / "ui"
    monkeypatch.setenv("AGENTENGINE_UI_DIR", str(ui_dir))
    monkeypatch.setenv("KSYUN_ACCOUNT_ID", "acct-1")
    monkeypatch.setenv("KS3_REGION", "cn-beijing-6")
    stored: dict[tuple[str, str], bytes] = {}

    async def fake_put(self, *, bucket, object_key, data, mime_type):
        assert bucket == "agentengine-acct-1-cn-beijing-6"
        assert object_key.startswith("agents/_runtime/attachments/")
        assert object_key.endswith(".png")
        assert mime_type == "image/png"
        stored[(bucket, object_key)] = data

    async def fake_read(self, *, bucket, object_key):
        return stored[(bucket, object_key)]

    monkeypatch.setattr(AttachmentStorageService, "_put_ks3_object", fake_put)
    monkeypatch.setattr(AttachmentStorageService, "_read_ks3_object", fake_read)

    transport = httpx.ASGITransport(app=server_app_module.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://ksadk.local") as client:
        upload_response = await client.post(
            "/agentengine/api/v1/UploadFile",
            files={"file": ("arch.png", b"\x89PNG\r\n\x1a\nruntime-ks3", "image/png")},
        )

        assert upload_response.status_code == 200
        file_uri = upload_response.json()["Data"]["FileData"]["fileUri"]
        file_id = file_uri.removeprefix("ksadk-upload://")
        local_file = ui_dir / "files" / f"{file_id}.png"
        local_file.unlink()

        content_response = await client.get(
            "/agentengine/api/v1/AttachmentContent",
            params={"FileUri": file_uri},
        )

    assert content_response.status_code == 200
    assert content_response.headers["content-type"].startswith("image/png")
    assert content_response.content == b"\x89PNG\r\n\x1a\nruntime-ks3"


def test_resolve_attachment_storage_path_restores_missing_local_cache_from_ks3(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("AGENTENGINE_UI_DIR", str(tmp_path / ".agentengine" / "ui"))
    monkeypatch.setenv("KSYUN_ACCOUNT_ID", "acct-1")
    monkeypatch.setenv("KS3_REGION", "cn-beijing-6")
    service = AttachmentStorageService()

    async def fake_put(self, **_kwargs):
        return None

    async def fake_read(self, *, bucket, object_key):
        assert bucket == "agentengine-acct-1-cn-beijing-6"
        assert object_key.startswith("agents/_runtime/attachments/")
        return b"restored"

    monkeypatch.setattr(AttachmentStorageService, "_put_ks3_object", fake_put)
    monkeypatch.setattr(AttachmentStorageService, "_read_ks3_object", fake_read)

    file_uri, local_path = service.store_sync(
        data=b"initial",
        file_id="abc123.png",
        display_name="abc.png",
        mime_type="image/png",
    )
    local_path.unlink()

    restored_path = resolve_attachment_storage_path(file_uri)

    assert restored_path == local_path
    assert restored_path.read_bytes() == b"restored"

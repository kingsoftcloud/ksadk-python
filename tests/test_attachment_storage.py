from __future__ import annotations

import importlib

import httpx
import pytest

from ksadk.conversations.attachment_storage import AttachmentStorageService
from ksadk.conversations.attachments import resolve_attachment_storage_path
from ksadk.conversations.normalize import normalize_parts_content
from ksadk.server.api_models import FileData, Part


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


def test_resolve_attachment_storage_path_downloads_hosted_ae_upload_via_kop(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("AGENTENGINE_UI_DIR", str(tmp_path / ".agentengine" / "ui"))
    calls = []

    class FakeResponse:
        status_code = 200
        headers = {
            "content-type": "text/markdown; charset=utf-8",
            "content-disposition": 'inline; filename="brief.md"',
        }
        content = b"# Brief\n\nHosted attachment body"

    def fake_action_raw_request(self, method, action, *, params=None, **_kwargs):
        calls.append({"method": method, "action": action, "params": params})
        return FakeResponse()

    monkeypatch.setattr(
        "ksadk.api.client.AgentEngineClient._action_raw_request",
        fake_action_raw_request,
    )

    restored_path = resolve_attachment_storage_path("ae-upload://hosted123.md")

    assert calls == [
        {
            "method": "GET",
            "action": "AttachmentContent",
            "params": {"FileUri": "ae-upload://hosted123.md"},
        }
    ]
    assert restored_path is not None
    assert restored_path.name == "hosted123.md"
    assert restored_path.read_bytes() == b"# Brief\n\nHosted attachment body"


def test_normalize_parts_content_reads_hosted_markdown_attachment_via_kop(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("AGENTENGINE_UI_DIR", str(tmp_path / ".agentengine" / "ui"))

    class FakeResponse:
        status_code = 200
        headers = {
            "content-type": "text/markdown",
            "content-disposition": 'inline; filename="brief.md"',
        }
        content = b"# Brief\n\nHosted attachment body"

    monkeypatch.setattr(
        "ksadk.api.client.AgentEngineClient._action_raw_request",
        lambda self, method, action, *, params=None, **_kwargs: FakeResponse(),
    )

    payload = normalize_parts_content(
        [
            Part(
                fileData=FileData(
                    fileUri="ae-upload://hosted123.md",
                    mimeType="text/markdown",
                    displayName="brief.md",
                )
            )
        ]
    )

    result = payload["attachment_results"][0]
    assert result["status"] == "ok"
    assert result["kind"] == "text"
    assert result["text"] == "# Brief\n\nHosted attachment body"
    assert "Hosted attachment body" in payload["content"]

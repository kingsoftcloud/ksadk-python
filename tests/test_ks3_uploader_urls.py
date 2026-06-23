import asyncio
import pickle
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

from ksadk.builders.ks3_uploader import KS3Uploader


def test_public_and_internal_url_by_key_include_full_object_key():
    uploader = KS3Uploader(region="cn-beijing-6", bucket="agentengine-test-cn-beijing-6")
    object_key = "agents/hr_projects_wrap_test/code_20260308154645.zip"

    public_url = uploader.get_public_url_by_key(object_key)
    internal_url = uploader.get_internal_url_by_key(object_key)

    assert public_url.endswith(f"/{object_key}")
    assert internal_url.endswith(f"/{object_key}")
    assert "code_20260308154645.zip" in public_url
    assert "code_20260308154645.zip" in internal_url


def test_url_by_key_normalizes_leading_slash():
    uploader = KS3Uploader(region="cn-beijing-6", bucket="agentengine-test-cn-beijing-6")
    object_key = "/agents/demo/code_20260308154645.zip"

    public_url = uploader.get_public_url_by_key(object_key)
    internal_url = uploader.get_internal_url_by_key(object_key)

    assert "//agents/" not in public_url
    assert "//agents/" not in internal_url
    assert public_url.endswith("/agents/demo/code_20260308154645.zip")
    assert internal_url.endswith("/agents/demo/code_20260308154645.zip")


def test_rank_upload_endpoints_prefers_fastest_reachable_host(monkeypatch):
    uploader = KS3Uploader(region="cn-beijing-6", bucket="agentengine-test-cn-beijing-6")

    monkeypatch.setattr(
        "ksadk.builders.ks3_uploader.get_ks3_endpoints",
        lambda _region: ("ks3-public.example.com", "ks3-internal.example.com"),
    )
    monkeypatch.setattr(
        uploader,
        "_probe_endpoint_latency",
        lambda host: {
            "ks3-public.example.com": 0.32,
            "ks3-internal.example.com": 0.08,
        }[host],
        raising=False,
    )

    targets, summary = uploader._rank_upload_endpoints()

    assert [item["host"] for item in targets] == [
        "ks3-internal.example.com",
        "ks3-public.example.com",
    ]
    assert "测速优先" in summary


def test_auto_rank_upload_endpoints_skips_unreachable_fallback(monkeypatch):
    uploader = KS3Uploader(region="cn-beijing-6", bucket="agentengine-test-cn-beijing-6")

    monkeypatch.setattr(
        "ksadk.builders.ks3_uploader.get_ks3_endpoints",
        lambda _region: ("ks3-public.example.com", "ks3-internal.example.com"),
    )
    monkeypatch.setattr(
        uploader,
        "_probe_endpoint_latency",
        lambda host: {
            "ks3-public.example.com": 0.04,
            "ks3-internal.example.com": None,
        }[host],
        raising=False,
    )

    targets, summary = uploader._rank_upload_endpoints()

    assert [item["host"] for item in targets] == ["ks3-public.example.com"]
    assert "跳过不可达端点" in summary


def test_large_upload_timeout_default_allows_slow_customer_networks(tmp_path):
    uploader = KS3Uploader(region="cn-beijing-6", bucket="agentengine-test-cn-beijing-6")
    artifact = tmp_path / "large.zip"
    with artifact.open("wb") as fp:
        fp.truncate(380 * 1024 * 1024)

    assert uploader._upload_timeout_seconds(artifact) >= 1800


def test_upload_timeout_env_override_still_wins(tmp_path, monkeypatch):
    uploader = KS3Uploader(region="cn-beijing-6", bucket="agentengine-test-cn-beijing-6")
    artifact = tmp_path / "large.zip"
    with artifact.open("wb") as fp:
        fp.truncate(380 * 1024 * 1024)
    monkeypatch.setenv("KS3_UPLOAD_TIMEOUT_SECONDS", "2400")

    assert uploader._upload_timeout_seconds(artifact) == 2400


def test_large_upload_uses_resumable_multipart_task(tmp_path, monkeypatch):
    uploader = KS3Uploader(region="cn-beijing-6", bucket="agentengine-test-cn-beijing-6")
    artifact = tmp_path / "large.zip"
    with artifact.open("wb") as fp:
        fp.truncate(120 * 1024 * 1024)

    captured = {}

    class _FakeConnection:
        def __init__(self, *args, **kwargs):
            captured["connection_kwargs"] = kwargs

    class _FakeKey:
        def __init__(self):
            self.name = "agents/demo/code.zip"

    class _FakeBucket:
        def new_key(self, object_key):
            captured["object_key"] = object_key
            return _FakeKey()

    class _FakeExecutor:
        def __init__(self, max_workers):
            captured["max_workers"] = max_workers

    class _FakeUploadTask:
        def __init__(self, key, bucket, src_file, executor, **kwargs):
            captured["task"] = {
                "key": key,
                "bucket": bucket,
                "src_file": src_file,
                "executor": executor,
                **kwargs,
            }

        def upload(self, headers=None):
            captured["headers"] = headers
            return SimpleNamespace(response_metadata=SimpleNamespace(status=200))

    monkeypatch.setenv("KSYUN_ACCESS_KEY", "ak")
    monkeypatch.setenv("KSYUN_SECRET_KEY", "sk")
    monkeypatch.setattr("ks3.connection.Connection", _FakeConnection)
    monkeypatch.setattr("ksadk.builders.ks3_uploader.ThreadPoolExecutor", _FakeExecutor)
    monkeypatch.setattr(uploader, "_ensure_bucket", lambda _conn: _FakeBucket(), raising=False)
    monkeypatch.setattr("ksadk.builders.ks3_uploader.UploadTask", _FakeUploadTask)

    assert uploader._upload_via_host(artifact, "agents/demo/code.zip", "ks3.example.com") is True
    assert captured["task"]["src_file"] == str(artifact)
    assert captured["task"]["resumable"] is True
    assert captured["task"]["resumable_filename"].endswith(".agentengine/ks3_resume/agents_demo_code.zip.ks3resume")


def test_ks3_resumable_upload_skips_already_uploaded_parts(tmp_path, monkeypatch):
    import ks3.upload as ks3_upload
    from ks3.multipart import MultiPartUpload, Part
    from ks3.upload import UploadRecord, UploadTask

    artifact = tmp_path / "large.zip"
    artifact.write_bytes(b"x" * (2 * 1024 * 1024))
    object_key = "agents/demo/code.zip"
    resumable_file = tmp_path / ".agentengine" / "ks3_resume" / "agents_demo_code.zip.ks3resume"
    resumable_file.parent.mkdir(parents=True, exist_ok=True)
    part_info_cls = getattr(ks3_upload, "PartInfo", None)
    if part_info_cls is not None:
        uploaded_part = part_info_cls(size=1024 * 1024, part_crc="crc1")
    else:
        uploaded_part = Part()
        uploaded_part.size = 1024 * 1024
        uploaded_part.part_crc = "crc1"

    record = UploadRecord(
        "upload-id",
        artifact.stat().st_size,
        artifact.stat().st_mtime,
        "bucket",
        object_key,
        1024 * 1024,
        {1: uploaded_part},
    )
    with resumable_file.open("wb") as fp:
        pickle.dump(record, fp)

    uploaded_parts = []

    class _FakeBucket:
        name = "bucket"
        connection = SimpleNamespace(
            enable_crc=True,
            provider=SimpleNamespace(checksum_crc64ecma_header="x-kss-checksum-crc64ecma"),
        )

    def _fake_upload_part_from_file(self, fp, part_num, headers=None):
        uploaded_parts.append(part_num)
        return SimpleNamespace(getheader=lambda _name: f"crc-{part_num}")

    def _fake_complete_upload(self, headers=None):
        return SimpleNamespace(
            response_metadata=SimpleNamespace(
                status=200,
                headers={"ETag": "etag", "x-kss-checksum-crc64ecma": "crc"},
                request_id="req",
            ),
            etag="etag",
        )

    monkeypatch.setattr(MultiPartUpload, "upload_part_from_file", _fake_upload_part_from_file, raising=False)
    monkeypatch.setattr(MultiPartUpload, "complete_upload", _fake_complete_upload, raising=False)

    task = UploadTask(
        key=SimpleNamespace(name=object_key),
        bucket=_FakeBucket(),
        src_file=str(artifact),
        executor=ThreadPoolExecutor(max_workers=2),
        part_size=1024 * 1024,
        resumable=True,
        resumable_filename=str(resumable_file),
    )

    result = task.upload(headers={})

    assert uploaded_parts == [2]
    assert result.response_metadata.status == 200
    assert not resumable_file.exists()


def test_upload_retries_next_endpoint_after_transport_failure(tmp_path, monkeypatch):
    uploader = KS3Uploader(region="cn-beijing-6", bucket="agentengine-test-cn-beijing-6")
    artifact = tmp_path / "demo.zip"
    artifact.write_bytes(b"zip")

    monkeypatch.setenv("KSYUN_ACCESS_KEY", "ak")
    monkeypatch.setenv("KSYUN_SECRET_KEY", "sk")
    monkeypatch.setattr(
        uploader,
        "_rank_upload_endpoints",
        lambda: (
            [
                {"host": "ks3-internal.example.com", "label": "内网"},
                {"host": "ks3-public.example.com", "label": "公网"},
            ],
            "测速优先 内网",
        ),
        raising=False,
    )

    calls = []

    def fake_upload_via_host(_file_path, _object_key, host):
        calls.append(host)
        if host == "ks3-internal.example.com":
            raise TimeoutError("internal timeout")
        return True

    monkeypatch.setattr(uploader, "_upload_via_host", fake_upload_via_host, raising=False)

    result = asyncio.run(uploader.upload(artifact, "agents/demo/code.zip"))

    assert result == "ks3://agentengine-test-cn-beijing-6/agents/demo/code.zip"
    assert calls == ["ks3-internal.example.com", "ks3-public.example.com"]

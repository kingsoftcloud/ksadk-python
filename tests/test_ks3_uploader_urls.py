import asyncio

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

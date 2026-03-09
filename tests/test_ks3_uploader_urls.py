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

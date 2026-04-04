import os


def _clear_kb_env(monkeypatch):
    for key in list(os.environ):
        if key.startswith("KSADK_KB_") or key.startswith("KSYUN_"):
            monkeypatch.delenv(key, raising=False)


def test_from_env_prefers_explicit_region_over_ksyun_region(monkeypatch):
    from ksadk.knowledge_base.client import KnowledgeBaseClient

    _clear_kb_env(monkeypatch)
    monkeypatch.setenv("KSADK_KB_DATASET_ID", "dataset-test")
    monkeypatch.setenv("KSYUN_REGION", "pre-online")
    monkeypatch.setenv("KSADK_KB_REGION", "cn-beijing-6")

    client = KnowledgeBaseClient.from_env()

    assert client.region == "cn-beijing-6"


def test_from_env_falls_back_to_ksyun_region(monkeypatch):
    from ksadk.knowledge_base.client import KnowledgeBaseClient

    _clear_kb_env(monkeypatch)
    monkeypatch.setenv("KSADK_KB_DATASET_ID", "dataset-test")
    monkeypatch.setenv("KSYUN_REGION", "pre-online")

    client = KnowledgeBaseClient.from_env()

    assert client.region == "pre-online"


def test_from_env_uses_http_for_inner_endpoint_when_scheme_unset(monkeypatch):
    from ksadk.knowledge_base.client import KnowledgeBaseClient

    _clear_kb_env(monkeypatch)
    monkeypatch.setenv("KSADK_KB_DATASET_ID", "dataset-test")
    monkeypatch.setenv("KSADK_KB_ENDPOINT", "aicp.inner.api.ksyun.com")

    client = KnowledgeBaseClient.from_env()

    assert client.scheme == "http"


def test_from_env_keeps_explicit_scheme_for_inner_endpoint(monkeypatch):
    from ksadk.knowledge_base.client import KnowledgeBaseClient

    _clear_kb_env(monkeypatch)
    monkeypatch.setenv("KSADK_KB_DATASET_ID", "dataset-test")
    monkeypatch.setenv("KSADK_KB_ENDPOINT", "aicp.inner.api.ksyun.com")
    monkeypatch.setenv("KSADK_KB_SCHEME", "https")

    client = KnowledgeBaseClient.from_env()

    assert client.scheme == "https"

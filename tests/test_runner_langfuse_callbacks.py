import importlib
import sys
import types


class _FakeLangfuse:
    instances = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.__class__.instances.append(kwargs)


class _FakeCallbackHandler:
    instances = []

    def __init__(self, *, public_key=None, trace_context=None):
        self.public_key = public_key
        self.trace_context = trace_context
        self.__class__.instances.append(self)


def _install_fake_langfuse(monkeypatch):
    _FakeLangfuse.instances.clear()
    _FakeCallbackHandler.instances.clear()
    monkeypatch.setitem(
        sys.modules,
        "langfuse",
        types.SimpleNamespace(Langfuse=_FakeLangfuse),
    )
    monkeypatch.setitem(
        sys.modules,
        "langfuse.langchain",
        types.SimpleNamespace(CallbackHandler=_FakeCallbackHandler),
    )


def _reload_module(monkeypatch):
    module = importlib.import_module("ksadk.runners.utils.langfuse")
    module = importlib.reload(module)
    monkeypatch.setattr(module, "_langfuse_callback", None)
    monkeypatch.setattr(module, "_cloud_monitor_langfuse_callback", None)
    return module


def test_get_langfuse_callbacks_returns_primary_and_cloud_monitor(monkeypatch):
    _install_fake_langfuse(monkeypatch)
    monkeypatch.setenv("LANGFUSE_USE_CALLBACK", "true")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-primary")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-primary")
    monkeypatch.setenv("LANGFUSE_BASE_URL", "https://trace-pre.example.com")
    monkeypatch.setenv("CLOUD_MONITOR_LANGFUSE_PUBLIC_KEY", "pk-cloud")
    monkeypatch.setenv("CLOUD_MONITOR_LANGFUSE_SECRET_KEY", "sk-cloud")
    monkeypatch.setenv("CLOUD_MONITOR_LANGFUSE_HOST", "https://cn-beijing-6.otlp.ksyun.com:4318")

    module = _reload_module(monkeypatch)

    callbacks = module.get_langfuse_callbacks()

    assert [callback.public_key for callback in callbacks] == ["pk-primary", "pk-cloud"]
    assert [
        {key: value for key, value in instance.items() if key != "tracer_provider"}
        for instance in _FakeLangfuse.instances
    ] == [
        {
            "public_key": "pk-primary",
            "secret_key": "sk-primary",
            "base_url": "https://trace-pre.example.com",
        },
        {
            "public_key": "pk-cloud",
            "secret_key": "sk-cloud",
            "base_url": "https://cn-beijing-6.otlp.ksyun.com:4318",
        },
    ]
    assert (
        _FakeLangfuse.instances[0]["tracer_provider"]
        is not _FakeLangfuse.instances[1]["tracer_provider"]
    )


def test_get_langfuse_callbacks_skips_incomplete_cloud_monitor_config(monkeypatch):
    _install_fake_langfuse(monkeypatch)
    monkeypatch.setenv("LANGFUSE_USE_CALLBACK", "true")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-primary")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-primary")
    monkeypatch.setenv("LANGFUSE_BASE_URL", "https://trace-pre.example.com")
    monkeypatch.setenv("CLOUD_MONITOR_LANGFUSE_PUBLIC_KEY", "pk-cloud")

    module = _reload_module(monkeypatch)

    callbacks = module.get_langfuse_callbacks()

    assert [callback.public_key for callback in callbacks] == ["pk-primary"]

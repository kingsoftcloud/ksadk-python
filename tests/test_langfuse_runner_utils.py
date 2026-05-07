from __future__ import annotations

import importlib
import sys
import types


class _FakeCallbackHandler:
    instances = 0

    def __init__(self):
        self.__class__.instances += 1


def _reload_langfuse_utils(monkeypatch):
    module = importlib.import_module("ksadk.runners.utils.langfuse")
    module = importlib.reload(module)
    monkeypatch.setattr(module, "_langfuse_callback", None)
    _FakeCallbackHandler.instances = 0
    monkeypatch.setitem(
        sys.modules,
        "langfuse.langchain",
        types.SimpleNamespace(CallbackHandler=_FakeCallbackHandler),
    )
    return module


def test_langfuse_callback_disabled_by_default_when_otlp_direct_is_available(monkeypatch):
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-test")
    monkeypatch.setenv("LANGFUSE_BASE_URL", "https://trace-pre.example.com")
    monkeypatch.delenv("LANGFUSE_USE_CALLBACK", raising=False)

    module = _reload_langfuse_utils(monkeypatch)

    assert module.get_langfuse_callback() is None
    assert _FakeCallbackHandler.instances == 0


def test_langfuse_callback_can_be_enabled_explicitly(monkeypatch):
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-test")
    monkeypatch.setenv("LANGFUSE_BASE_URL", "https://trace-pre.example.com")
    monkeypatch.setenv("LANGFUSE_USE_CALLBACK", "true")

    module = _reload_langfuse_utils(monkeypatch)

    assert isinstance(module.get_langfuse_callback(), _FakeCallbackHandler)
    assert _FakeCallbackHandler.instances == 1

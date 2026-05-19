from __future__ import annotations

import importlib
import sys

import fastapi


def test_server_app_imports_when_fastapi_removes_add_event_handler(monkeypatch):
    original_fastapi = fastapi.FastAPI

    class FastAPIWithoutAddEventHandler(original_fastapi):
        def __getattribute__(self, name):
            if name == "add_event_handler":
                raise AttributeError(
                    "'FastAPI' object has no attribute 'add_event_handler'"
                )
            return super().__getattribute__(name)

    monkeypatch.setattr(fastapi, "FastAPI", FastAPIWithoutAddEventHandler)
    sys.modules.pop("ksadk.server", None)
    sys.modules.pop("ksadk.server.app", None)

    module = importlib.import_module("ksadk.server.app")

    assert module.app is not None


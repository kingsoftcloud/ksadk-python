import sys
from types import SimpleNamespace

from ksadk.tui import clipboard


def test_clipboard_copy_methods_skip_osc52_on_windows(monkeypatch):
    fake_pyperclip = SimpleNamespace(copy=lambda _text: None)
    app = SimpleNamespace(copy_to_clipboard=lambda _text: None)

    monkeypatch.setattr(clipboard.os, "name", "nt", raising=False)
    monkeypatch.setitem(sys.modules, "pyperclip", fake_pyperclip)

    methods = clipboard._clipboard_copy_methods(app)

    assert clipboard._copy_osc52 not in methods
    assert methods == [fake_pyperclip.copy, app.copy_to_clipboard]

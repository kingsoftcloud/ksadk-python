import importlib.util
import sys
import types
from pathlib import Path


def _load_chainlit_app_module():
    cl_stub = types.SimpleNamespace()

    def _decorator(func=None):
        if func is None:
            return lambda fn: fn
        return func

    class _Message:
        def __init__(self, content=""):
            self.content = content

    class _Step:
        def __init__(self, *args, **kwargs):
            pass

    class _UserSession:
        def get(self, *_args, **_kwargs):
            return None

        def set(self, *_args, **_kwargs):
            return None

    cl_stub.Message = _Message
    cl_stub.Step = _Step
    cl_stub.user_session = _UserSession()
    cl_stub.on_chat_start = _decorator
    cl_stub.on_message = _decorator
    cl_stub.on_stop = _decorator

    sys.modules["chainlit"] = cl_stub

    app_path = Path(__file__).resolve().parents[1] / "ksadk" / "chainlit" / "app.py"
    spec = importlib.util.spec_from_file_location("ksadk_chainlit_app_test", app_path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def test_normalize_markdown_inserts_newline_before_inline_heading():
    module = _load_chainlit_app_module()
    raw = "收到简历分析完成。# 六、面试推荐意见\n后续说明"

    normalized = module._normalize_markdown(raw)

    assert "。# 六、面试推荐意见" not in normalized
    assert "\n# 六、面试推荐意见" in normalized

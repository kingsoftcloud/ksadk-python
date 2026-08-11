"""HarnessApp codex runtime 单测:runtime: codex 配置解析 + adapter/build_app 分流。

不打网络:CodexRuntimeAdapter 构造 client 只验类型/装配,
不真正跑 turn(那是浏览器 e2e 的事)。缺 openai_codex 时 importorskip 跳过。
"""

import pytest

from ksadk.harness import HarnessApp, HarnessConfig, HarnessConfigError

codex_sdk = pytest.importorskip("openai_codex")  # noqa: F841  缺 ksadk[codex] 跳过


def _write(tmp_path, text):
    p = tmp_path / "harness_codex.yaml"
    p.write_text(text, encoding="utf-8")
    return p


def test_config_runtime_codex_parsed():
    cfg = HarnessConfig.from_dict(
        {"model": "glm-5.2", "prompt": "你是助手", "runtime": "codex"}
    )
    assert cfg.runtime == "codex"


def test_config_runtime_default_yaml():
    cfg = HarnessConfig.from_dict({"model": "m", "prompt": "p"})
    assert cfg.runtime == "yaml"


def test_config_runtime_invalid_rejected(tmp_path):
    with pytest.raises(HarnessConfigError, match="runtime"):
        HarnessApp.from_yaml(
            _write(tmp_path, "model: m\nprompt: p\nruntime: bogus\n")
        )


def test_build_adapter_codex(tmp_path):
    """runtime: codex -> adapter 返回唯一 CodexRuntimeAdapter。"""
    from ksadk.codex import CodexRuntimeAdapter

    app = HarnessApp.from_yaml(
        _write(tmp_path, "model: glm-5.2\nprompt: 你是助手\nruntime: codex\n")
    )
    assert isinstance(app.adapter(), CodexRuntimeAdapter)


def test_build_app_codex_runtime_type(tmp_path):
    """runtime: codex -> build_app 装配显式 Executor/Context。"""
    from ksadk.codex import CodexRuntimeAdapter

    app = HarnessApp.from_yaml(
        _write(tmp_path, "model: glm-5.2\nprompt: 你是助手\nruntime: codex\n")
    )
    fastapi_app = app.build_app()
    assert isinstance(app.adapter(), CodexRuntimeAdapter)
    assert fastapi_app.state.runtime.executor is app.executor
    assert fastapi_app.state.runtime.launch_context.runtime_type == "codex"
    assert not hasattr(fastapi_app.state.runtime, "runner")

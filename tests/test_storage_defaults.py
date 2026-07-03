import pytest

from ksadk.cli.storage import (
    DEFAULT_STORAGE_SIZE_GI,
    build_storage_config,
    resolve_default_storage_mount_path,
    validate_storage_mount_path,
    validate_storage_size_gi,
)


def test_resolve_default_storage_mount_path_for_frameworks():
    assert resolve_default_storage_mount_path("adk") == "/home/node/.agentengine"
    assert resolve_default_storage_mount_path("langchain") == "/home/node/.agentengine"
    assert resolve_default_storage_mount_path("langgraph") == "/home/node/.agentengine"
    assert resolve_default_storage_mount_path("deepagents") == "/home/node/.agentengine"
    assert resolve_default_storage_mount_path("hermes") == "/home/node/.hermes"
    assert resolve_default_storage_mount_path("openclaw") == "/home/node/.openclaw"


def test_validate_storage_size_gi_enforces_range():
    assert validate_storage_size_gi(None) == DEFAULT_STORAGE_SIZE_GI
    assert validate_storage_size_gi(20) == 20
    assert validate_storage_size_gi(500) == 500
    with pytest.raises(Exception):
        validate_storage_size_gi(19)
    with pytest.raises(Exception):
        validate_storage_size_gi(501)


def test_validate_storage_mount_path_requires_absolute_path():
    assert validate_storage_mount_path("/home/node/.hermes/") == "/home/node/.hermes"
    with pytest.raises(Exception):
        validate_storage_mount_path("relative/path")
    with pytest.raises(Exception):
        validate_storage_mount_path("/")


def test_build_storage_config_defaults_for_serverless_targets():
    assert build_storage_config("hermes", target="serverless") == {
        "mount_path": "/home/node/.hermes",
        "size_gi": 20,
    }
    assert build_storage_config("openclaw", target="serverless") == {
        "mount_path": "/home/node/.openclaw",
        "size_gi": 20,
    }
    # 非 hermes/openclaw 框架默认不挂盘（产品要求 adk/langgraph 这类默认不传挂盘参数）
    assert build_storage_config("langgraph", target="kce") is None
    assert build_storage_config("adk", target="serverless") is None
    assert build_storage_config("langchain", target="serverless") is None
    assert build_storage_config("deepagents", target="serverless") is None
    # 用户显式指定 --storage-mount-path 时，任意框架都挂
    assert build_storage_config("langgraph", target="serverless", mount_path="/data") == {
        "mount_path": "/data",
        "size_gi": 20,
    }
    assert build_storage_config("adk", target="kce", mount_path="/home/node/.agentengine") == {
        "mount_path": "/home/node/.agentengine",
        "size_gi": 20,
    }
    # 仅显式 size、未指定 mount_path 时，非默认框架仍不挂（mount_path 才是挂盘意图的判据）
    assert build_storage_config("langgraph", target="serverless", size_gi=50) is None
    assert build_storage_config("langgraph", target="serverless", no_storage=True) is None
    assert build_storage_config("langgraph", target="docker") is None

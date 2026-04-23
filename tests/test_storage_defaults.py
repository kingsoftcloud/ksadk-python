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
    assert build_storage_config("langgraph", target="kce") == {
        "mount_path": "/home/node/.agentengine",
        "size_gi": 20,
    }
    assert build_storage_config("langgraph", target="serverless", no_storage=True) is None
    assert build_storage_config("langgraph", target="docker") is None

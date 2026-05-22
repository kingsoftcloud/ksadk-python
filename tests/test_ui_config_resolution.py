from ksadk.deployment.ui_config import is_same_origin, resolve_ui_config


def test_langgraph_defaults_to_chat_ui_path():
    cfg = resolve_ui_config(
        framework="langgraph",
        state={},
        cli_profile=None,
        cli_path=None,
        cli_url=None,
    )

    assert cfg.profile == "langchain"
    assert cfg.path == "/chat"
    assert cfg.url is None


def test_hermes_defaults_to_chat_ui_path():
    cfg = resolve_ui_config(
        framework="hermes",
        state={},
        cli_profile=None,
        cli_path=None,
        cli_url=None,
    )

    assert cfg.profile == "hermes"
    assert cfg.path == "/chat"
    assert cfg.url is None


def test_state_ui_config_applies_when_cli_not_set():
    cfg = resolve_ui_config(
        framework="adk",
        state={
            "ui_profile": "custom",
            "ui_path": "/dashboard",
            "ui_url": "https://ui.example.com/dashboard",
        },
        cli_profile=None,
        cli_path=None,
        cli_url=None,
    )

    assert cfg.profile == "custom"
    assert cfg.path == "/dashboard"
    assert cfg.url == "https://ui.example.com/dashboard"


def test_cli_overrides_state_and_can_clear_ui_url():
    cfg = resolve_ui_config(
        framework="langchain",
        state={
            "ui_profile": "custom",
            "ui_path": "/custom",
            "ui_url": "https://ui.example.com/custom",
        },
        cli_profile="langchain",
        cli_path="/",
        cli_url="",
    )

    assert cfg.profile == "langchain"
    assert cfg.path == "/"
    assert cfg.url is None


def test_legacy_langchain_state_path_is_migrated_to_chat():
    cfg = resolve_ui_config(
        framework="langgraph",
        state={
            "ui_profile": "langchain",
            "ui_path": "/langchain",
        },
        cli_profile=None,
        cli_path=None,
        cli_url=None,
    )

    assert cfg.profile == "langchain"
    assert cfg.path == "/chat"


def test_legacy_root_state_path_is_migrated_to_chat_for_managed_profiles():
    cfg = resolve_ui_config(
        framework="langgraph",
        state={
            "ui_profile": "langchain",
            "ui_path": "/",
        },
        cli_profile=None,
        cli_path=None,
        cli_url=None,
    )

    assert cfg.profile == "langchain"
    assert cfg.path == "/chat"


def test_same_origin_requires_scheme_and_netloc_match():
    assert is_same_origin("https://a.example.com/path", "https://a.example.com/")
    assert not is_same_origin("https://a.example.com/path", "http://a.example.com/")
    assert not is_same_origin("https://a.example.com/path", "https://b.example.com/")

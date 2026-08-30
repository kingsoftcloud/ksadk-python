from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

TESTS_ROOT = Path(__file__).resolve().parent
REPO_ROOT = TESTS_ROOT.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from ksadk.cli.ui import OUTPUT_MODE_PRETTY, configure_ui_runtime  # noqa: E402


_MODEL_ROUTE_ENV_NAMES = (
    "KSADK_CODEX_USE_PROXY",
    "KSADK_PROXY_UPSTREAM_BASE",
    "KSADK_PROXY_UPSTREAM_KEY",
    "OPENAI_BASE_URL",
    "OPENAI_API_BASE",
    "OPENAI_API_KEY",
    "OPENAI_MODEL_NAME",
    "MODEL_NAME",
)
_INITIAL_MODEL_ROUTE_ENV = {
    name: os.environ.get(name) for name in _MODEL_ROUTE_ENV_NAMES
}


def _restore_model_route_env() -> None:
    for name, value in _INITIAL_MODEL_ROUTE_ENV.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value


@pytest.fixture(autouse=True)
def reset_model_route_environment():
    """Keep provider/proxy choices isolated from collection and peer tests."""

    _restore_model_route_env()
    yield
    _restore_model_route_env()


@pytest.fixture(autouse=True)
def reset_cli_ui_runtime():
    os.environ.pop("AGENTENGINE_OUTPUT_MODE", None)
    os.environ.pop("AGENTENGINE_NO_COLOR", None)
    os.environ.pop("AGENTENGINE_GLOBAL_DRY_RUN", None)
    configure_ui_runtime(output_mode=OUTPUT_MODE_PRETTY, no_color=False)
    yield
    os.environ.pop("AGENTENGINE_OUTPUT_MODE", None)
    os.environ.pop("AGENTENGINE_NO_COLOR", None)
    os.environ.pop("AGENTENGINE_GLOBAL_DRY_RUN", None)
    configure_ui_runtime(output_mode=OUTPUT_MODE_PRETTY, no_color=False)


@pytest.fixture(autouse=True)
def reset_models_catalog_cache():
    """_build_models_payload 有 60s 进程内缓存，测试间必须隔离。"""
    from ksadk.server.routes.workspace import _MODELS_CATALOG_CACHE

    _MODELS_CATALOG_CACHE.clear()
    yield
    _MODELS_CATALOG_CACHE.clear()

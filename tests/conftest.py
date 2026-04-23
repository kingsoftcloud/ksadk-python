from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

TESTS_ROOT = Path(__file__).resolve().parent
REPO_ROOT = TESTS_ROOT.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from ksadk.cli.ui import OUTPUT_MODE_PRETTY, configure_ui_runtime


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

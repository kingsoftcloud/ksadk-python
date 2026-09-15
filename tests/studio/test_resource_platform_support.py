"""An optional Unix resource host must not prevent Studio from starting."""

import os
import subprocess
import sys
from pathlib import Path


def test_studio_starts_without_fcntl_and_resource_writes_fail_closed(tmp_path):
    script = r'''
import sys
sys.modules["fcntl"] = None
from pathlib import Path
from ksadk.studio.api import create_studio_app
from ksadk.studio.errors import StudioError
from ksadk.studio.model_client import CredentialResolver
from ksadk.studio.resource_connections import ResourceConnectionRepository
from ksadk.studio.workspace import Workspace

root = Path(sys.argv[1])
app = create_studio_app(root, session_token="fixture-session")
assert any(route.path == "/api/v1/resource-connections" for route in app.routes)
repository = ResourceConnectionRepository(Workspace(root), CredentialResolver())
try:
    repository.list()
except StudioError as error:
    assert error.code == "RESOURCE_CONNECTION_PLATFORM_UNSUPPORTED"
    assert error.status_code == 501
else:
    raise AssertionError("Resource access must not fall back to unlocked I/O")
assert not repository.root.exists()
'''
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path)],
        cwd=Path(__file__).resolve().parents[2],
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[2])},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr

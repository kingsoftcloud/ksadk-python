from pathlib import Path


def test_openclaw_workspace_files_capability_stays_disabled_until_gateway_path_is_verified():
    source = Path("deploy/openclaw/bootstrap.sh").read_text(encoding="utf-8")

    assert 'export OPENCLAW_WORKSPACE_FILES_ENABLED="${OPENCLAW_WORKSPACE_FILES_ENABLED:-0}"' in source
    assert "TODO(workspace-files-v1): enable OpenClaw workspace files after loopback gateway forwarding is verified end-to-end." in source

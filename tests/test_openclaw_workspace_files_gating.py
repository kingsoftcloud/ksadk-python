from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_openclaw_workspace_files_bootstrap_flag_is_explicit():
    source = (REPO_ROOT / "deploy" / "openclaw" / "bootstrap.sh").read_text(encoding="utf-8")

    assert 'export OPENCLAW_WORKSPACE_FILES_ENABLED="${OPENCLAW_WORKSPACE_FILES_ENABLED:-0}"' in source

import os

import pytest

from ksadk.plugins.teams.artifacts import read_workspace_artifact
from ksadk.plugins.teams.errors import TeamsError


def test_artifact_reads_exact_workspace_file_and_rejects_symlinks(tmp_path):
    root = tmp_path / "member"
    root.mkdir()
    (root / "result.md").write_text("本次结果", encoding="utf-8")
    assert read_workspace_artifact(root, "result.md") == ("result.md", "本次结果".encode())
    outside = tmp_path / "private.txt"
    outside.write_text("must not read")
    (root / "escape.txt").symlink_to(outside)
    (root / "escape-dir").symlink_to(tmp_path, target_is_directory=True)
    for path in ("../private.txt", "escape.txt", "escape-dir/private.txt", str(outside)):
        with pytest.raises(TeamsError):
            read_workspace_artifact(root, path)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="requires POSIX")
def test_nonregular_artifact_does_not_block_reader(tmp_path):
    os.mkfifo(tmp_path / "pipe")
    with pytest.raises(TeamsError, match="普通文件"):
        read_workspace_artifact(tmp_path, "pipe")

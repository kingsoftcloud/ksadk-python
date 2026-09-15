"""Package-manager children must stop before the bridge rolls back a profile."""

import os
import sys
import time

import pytest

from ksadk.plugins.bridges import dsh


@pytest.mark.skipif(os.name != "posix", reason="DSH production process groups require Unix")
@pytest.mark.parametrize("parent_exits", [False, True])
def test_timeout_stops_package_child_before_profile_rollback(tmp_path, monkeypatch, parent_exits):
    marker = tmp_path / "late-profile-mutation"
    child_pid = tmp_path / "child.pid"
    child = (
        "import pathlib,time; time.sleep(1); "
        f"pathlib.Path({str(marker)!r}).write_text('mutation')"
    )
    parent = (
        "import pathlib,subprocess,sys,time; "
        f"p=subprocess.Popen([sys.executable, '-c', {child!r}]); "
        f"pathlib.Path({str(child_pid)!r}).write_text(str(p.pid)); "
        + ("sys.exit(0)" if parent_exits else "time.sleep(10)")
    )
    monkeypatch.setattr(dsh, "_COMMAND_TIMEOUT_SECONDS", 0.4)
    try:
        with pytest.raises(dsh.DshHostUnavailableError):
            dsh.DshProfilePluginBridge._run_command(
                [sys.executable, "-I", "-c", parent], tmp_path, {},
            )
        assert child_pid.exists(), "The descendant must actually start for this regression"
        # A surviving pnpm-like child would mutate the restored profile here.
        time.sleep(1)
        assert not marker.exists()
    finally:
        if child_pid.exists():
            try:
                os.kill(int(child_pid.read_text()), 9)
            except ProcessLookupError:
                pass


def test_command_preserves_success_output_and_redacts_failure(tmp_path):
    result = dsh.DshProfilePluginBridge._run_command(
        [sys.executable, "-I", "-c", "print('profile-result')"], tmp_path, {},
    )
    assert result.stdout == "profile-result\n"
    with pytest.raises(dsh.DshPluginMutationError) as error:
        dsh.DshProfilePluginBridge._run_command(
            [
                sys.executable, "-I", "-c",
                "import sys; sys.stderr.write('token=fake-secret'); sys.exit(2)",
            ],
            tmp_path, {},
        )
    assert "fake-secret" not in str(error.value)
    assert "exit code 2" in str(error.value)

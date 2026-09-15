from __future__ import annotations

import pytest

from ksadk.harness.sandbox import HarnessSandboxExecutor, SandboxPolicyDenied


@pytest.mark.asyncio
async def test_read_only_policy_executes_reads_and_denies_side_effects(tmp_path, capsys):
    (tmp_path / "facts.txt").write_text("read-ok", encoding="utf-8")
    sandbox = HarnessSandboxExecutor(workspace_root=tmp_path, read_only=True)

    result = await sandbox.run_command("cat facts.txt")
    assert result["ok"] is True
    assert result["stdout"] == "read-ok"

    denied = []
    for command in (
        "touch created.txt",
        "curl https://example.com",
        "rm facts.txt",
        "cat facts.txt > copied.txt",
    ):
        with pytest.raises(SandboxPolicyDenied, match="read-only") as exc_info:
            await sandbox.run_command(command)
        denied.append(str(exc_info.value))

    evidence = f"sandbox_deny_reason={denied[0]}"
    with capsys.disabled():
        print(evidence)
    assert not (tmp_path / "created.txt").exists()
    assert not (tmp_path / "copied.txt").exists()
    assert (tmp_path / "facts.txt").read_text(encoding="utf-8") == "read-ok"
    assert "sandbox_deny_reason=" in evidence


@pytest.mark.asyncio
async def test_read_policy_confines_paths_to_workspace(tmp_path):
    sandbox = HarnessSandboxExecutor(workspace_root=tmp_path, read_only=True)
    with pytest.raises(SandboxPolicyDenied, match="workspace"):
        await sandbox.run_command("cat ../outside.txt")

"""Project-local OAuth storage, independence from legacy state and artifact exclusion."""

import json
import os
import stat
import subprocess
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from ksadk.mcp_runtime.oauth.local.persistence import LocalVault, open_project_vault
from ksadk.mcp_runtime.oauth.local.runtime import LocalOAuthRuntime
from ksadk.mcp_runtime.oauth.security import digest


@pytest.fixture
def locations(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    project = tmp_path / "项目 with spaces"
    project.mkdir()
    legacy = home / ".ksadk" / "mcp-oauth" / digest(str(project.resolve()))
    current = project / ".agentengine" / "mcp-oauth"
    return project, legacy, current


def test_default_directory_is_project_local_and_override_is_preserved(locations):
    project, legacy, current = locations
    local = SimpleNamespace(launch=SimpleNamespace(project_dir=project))
    assert LocalOAuthRuntime(local).directory == current
    assert LocalOAuthRuntime(local, directory=legacy).directory == legacy
    assert not current.exists()  # Merely constructing the runtime never stores credentials.


def test_new_project_ignores_vault_and_keeps_other_ignore_rules(locations):
    project, legacy, current = locations
    current.parent.mkdir()
    ignore = current.parent / ".gitignore"
    ignore.write_text("/existing-file")
    subprocess.run(["git", "init", "-q", str(project)], check=True)
    vault = open_project_vault(project)
    vault.save()
    vault.close()
    again = open_project_vault(project)
    again.close()
    assert "/existing-file\n" in ignore.read_text()
    assert ignore.read_text().count("/mcp-oauth/\n") == 1
    for name in ("key", "vault.sqlite3", "owner.lock"):
        result = subprocess.run(
            ["git", "check-ignore", "-q", f".agentengine/mcp-oauth/{name}"], cwd=project
        )
        assert result.returncode == 0
        if os.name != "nt":
            assert stat.S_IMODE((current / name).stat().st_mode) == 0o600
    if os.name != "nt":
        assert stat.S_IMODE(current.stat().st_mode) == 0o700
    assert not legacy.exists()


@pytest.mark.parametrize(("before", "expected"), [
    (b"\xef\xbb\xbf/mcp-oauth/\r\n", b"\xef\xbb\xbf/mcp-oauth/\r\n"),
    (b"# \xd6\xd0\xce\xc4\r\n/other", b"# \xd6\xd0\xce\xc4\r\n/other\r\n/mcp-oauth/\r\n"),
])
def test_vault_keeps_existing_gitignore_bytes(locations, before, expected):
    project, _, current = locations
    current.parent.mkdir()
    ignore = current.parent / ".gitignore"
    ignore.write_bytes(before)
    open_project_vault(project).close()
    assert ignore.read_bytes() == expected


@pytest.mark.skipif(os.name != "nt", reason="requires native Windows file ACLs")
def test_existing_windows_vault_protects_key_and_files(locations):
    project, _, current = locations
    # Start with an existing directory inheriting its parent project's ACL.
    current.mkdir(parents=True)
    vault = open_project_vault(project)
    vault.save()
    vault.close()
    script = """
        $ErrorActionPreference = 'Stop'
        $paths = @($env:TEST_VAULT) + @(Get-ChildItem -LiteralPath $env:TEST_VAULT -Name |
            ForEach-Object { Join-Path $env:TEST_VAULT $_ })
        foreach ($path in $paths) {
            $acl = Get-Acl -LiteralPath $path
            if (-not $acl.AreAccessRulesProtected) { throw 'Vault ACL inherits parent access' }
            $sids = @($acl.GetAccessRules($true,$true,[System.Security.Principal.SecurityIdentifier]) |
                ForEach-Object { $_.IdentityReference.Value })
            ConvertTo-Json -Compress -InputObject $sids
        }
    """
    checked = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
        env={**os.environ, "TEST_VAULT": str(current)}, check=True, capture_output=True, text=True,
    )
    rules = [set(json.loads(line)) for line in checked.stdout.splitlines()]
    assert len(rules) >= 4
    assert all(sids == {"S-1-3-4", "S-1-5-32-544", "S-1-5-18"} for sids in rules)


def test_old_grant_is_ignored_even_while_old_process_owns_it(locations):
    project, legacy, current = locations
    old = LocalVault(legacy)
    old.data["grants"]["old"] = {"access_token": "fixture-old"}
    old.save()
    disk_state = {p.name: p.read_bytes() for p in legacy.iterdir()}
    try:
        fresh = open_project_vault(project)
        assert fresh.data["grants"] == {}
        fresh.data["grants"]["new"] = {"access_token": "fixture-new"}
        fresh.save()
        fresh.close()
        reopened = open_project_vault(project)
        assert set(reopened.data["grants"]) == {"new"}
        reopened.close()
        assert b"fixture-new" not in (current / "vault.sqlite3").read_bytes()
    finally:
        old.close()
    assert {p.name: p.read_bytes() for p in legacy.iterdir()} == disk_state


def test_custom_web_bundle_cannot_serve_the_vault(locations, monkeypatch):
    from ksadk.server.routes import common

    project, _, current = locations
    vault = open_project_vault(project)
    vault.close()
    index = project / "index.html"
    index.write_text("public UI")
    monkeypatch.setattr(
        common,
        "_resolve_agent_ui_spec",
        lambda: {
            "enabled": True,
            "source": "custom",
            "path": "/",
            "bundle_path": project,
            "index_path": index,
        },
    )
    for name in ("key", "vault.sqlite3", "owner.lock"):
        response = common._resolve_ui_static_response(f"/.agentengine/mcp-oauth/{name}")
        assert response is None or Path(response.path).resolve() == index
    assert Path(common._resolve_ui_static_response("/").path) == index
    index.unlink()
    index.symlink_to(current / "key")
    assert common._resolve_ui_static_response("/") is None


@pytest.mark.parametrize("mode", ["code", "container", "mcp-container"])
def test_build_artifacts_exclude_project_and_nested_local_vaults(tmp_path, mode):
    from ksadk.builders.code_builder import CodeBuilder
    from ksadk.builders.container_builder import ContainerBuilder
    from ksadk.detection import DetectionResult, FrameworkType

    project = tmp_path / "project"
    package = project / "demo"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (package / "agent.py").write_text("root_agent = object()\n")
    for root in (project, project / "examples" / "nested"):
        directory = root / ".agentengine" / "mcp-oauth"
        directory.mkdir(parents=True)
        for name in ("key", "vault.sqlite3", "owner.lock"):
            (directory / name).write_text("LOCAL-OAUTH-PRIVATE-FIXTURE")
    detection = DetectionResult(
        type=FrameworkType.LANGGRAPH,
        name="demo",
        entry_point="demo/agent.py",
        package_path=str(package),
        agent_variable="root_agent",
        confidence=1.0,
    )
    if mode == "code":
        builder = CodeBuilder(project)
        builder.build_dir.mkdir(parents=True, exist_ok=True)
        builder.deps_dir.mkdir(parents=True, exist_ok=True)
        archive = tmp_path / "agent.zip"
        builder._package_zip(archive, detection)
        with zipfile.ZipFile(archive) as bundle:
            assert "demo/agent.py" in bundle.namelist()
            assert not any(".agentengine" in Path(name).parts for name in bundle.namelist())
            assert all(
                b"LOCAL-OAUTH-PRIVATE-FIXTURE" not in bundle.read(n) for n in bundle.namelist()
            )
    else:
        if mode == "mcp-container":
            from ksadk.builders.mcp_builder import MCPContainerBuilder
            from ksadk.detection.mcp_detector import MCPDetectionResult

            result = MCPContainerBuilder(project)._package_mcp_project(
                MCPDetectionResult(True, "demo", "demo/agent.py", str(package))
            )
        else:
            result = ContainerBuilder(project)._package(detection)
        context = Path(result.build_dir)
        assert (context / "demo" / "agent.py").exists()
        assert not any(p.name == ".agentengine" for p in context.rglob("*"))
        assert all(
            b"LOCAL-OAUTH-PRIVATE-FIXTURE" not in p.read_bytes()
            for p in context.rglob("*")
            if p.is_file()
        )

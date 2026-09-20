"""Exercise the DMG Make target without signing or invoking disk utilities."""
from pathlib import Path
import os
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("image_exit", [0, 17])
def test_existing_bundle_is_not_rebuilt_and_image_errors_propagate(tmp_path, image_exit):
    app = tmp_path / "signed.app"
    app.mkdir()
    ticket = app / "ticket"
    ticket.write_text("notarized original")
    commands = tmp_path / "commands"
    commands.mkdir()
    calls = tmp_path / "calls"
    for name, script in {
        "ditto": '#!/bin/sh\nprintf "copy\\n" >> "$CALLS"\ncp -R "$1" "$2"\n',
        "hdiutil": '#!/bin/sh\nprintf "image\\n" >> "$CALLS"\nexit "$IMAGE_EXIT"\n',
    }.items():
        executable = commands / name
        executable.write_text(script)
        executable.chmod(0o755)
    result = subprocess.run(
        ["make", "--no-print-directory", "studio-app-dmg-existing",
         f"STUDIO_APP_BUNDLE={app}", f"STUDIO_APP_DMG={tmp_path / 'test.dmg'}"],
        cwd=ROOT,
        env={**os.environ, "PATH": f"{commands}{os.pathsep}{os.environ['PATH']}",
             "CALLS": str(calls), "IMAGE_EXIT": str(image_exit)},
        capture_output=True, text=True,
    )
    assert ticket.read_text() == "notarized original"
    assert calls.read_text().splitlines() == ["copy", "image"]
    assert (result.returncode == 0) == (image_exit == 0), result.stdout + result.stderr
    if image_exit:
        assert "✅ Studio DMG" not in result.stdout


def test_packaging_uses_requested_version_once():
    result = subprocess.run(
        ["make", "-n", "-o", "build-wheel", "studio-app-package", "STUDIO_APP_VERSION=0.8.5"],
        cwd=ROOT, capture_output=True, text=True, check=True,
    )
    assert 'STUDIO_APP_VERSION="0.8.5"' in result.stdout
    assert 'STUDIO_APP_VERSION="0.8.50.8.5"' not in result.stdout


def test_macos_runtime_relocates_python_framework_and_checks_startup():
    script = (ROOT / "scripts" / "package_studio_app.sh").read_text()
    makefile = (ROOT / "Makefile").read_text()
    workflow = (ROOT / ".github" / "workflows" / "release-studio-app.yml").read_text()
    assert "install_name_tool -change" in script
    assert '"@loader_path/../lib/$bundled_python_name"' in script
    assert '"@loader_path/../../../../lib/$bundled_python_name"' in script
    assert 'Resources/Python.app' in script
    assert 'for stdlib_tree in test idlelib turtledemo tkinter ensurepip' in script
    assert "install_name_tool -id" in script
    assert "otool -L" in makefile
    assert '$(STUDIO_APP_RUNTIME)"/bin/python*' in makefile
    macos_job = workflow.split("# Windows x64:", 1)[0]
    assert '"$app/Contents/Resources/runtime/bin/python3"' in macos_job
    assert "AgentKitStudio-macos-arm64.zip" not in macos_job
    assert "Save verified DMG for manual validation" in macos_job

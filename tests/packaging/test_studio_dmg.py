"""Exercise the DMG Make target without signing or invoking disk utilities."""
import os
import re
import subprocess
import shlex
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("script_name", ["package_studio_app.sh", "package_studio_app_windows.sh"])
@pytest.mark.parametrize("override", [None, "0.3.42"])
def test_make_exports_web_version_to_desktop_manifest(tmp_path, script_name, override):
    script = (ROOT / "scripts" / script_name).read_text()
    expression = re.search(r'"ksadk_web_version": "([^"]+)"', script).group(1)
    probe = tmp_path / "probe.mk"
    probe.write_text("probe-web-manifest:\n\t@printf '%s' \""
                     + expression.replace("$", "$$") + "\"\n")
    environment = dict(os.environ)
    environment.pop("KSADK_WEB_VERSION", None)
    command = ["make", "--no-print-directory", "-f", "Makefile", "-f", str(probe),
               "probe-web-manifest"]
    if override is not None:
        command.append(f"KSADK_WEB_VERSION={override}")
    result = subprocess.run(command, cwd=ROOT, env=environment,
                            capture_output=True, text=True, check=True)
    expected = subprocess.check_output(
        ["python3", "scripts/reviewed_web_assets.py", "identity", "--field", "package_version"],
        cwd=ROOT, text=True).strip()
    # Metadata describes the actual package even if a caller sets another label.
    assert result.stdout == expected


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
    assert '"@loader_path/../../../../$bundled_python_name"' in script
    assert 'lib/Resources/Python.app' in script
    assert 'codesign --force --sign - "$python_library"' in script
    assert 'for stdlib_tree in test idlelib turtledemo tkinter ensurepip' in script
    assert "install_name_tool -id" in script
    assert "otool -L" in makefile
    assert '$(STUDIO_APP_RUNTIME)"/bin/python*' in makefile
    macos_job = workflow.split("# Windows x64:", 1)[0]
    assert "runs-on: macos-15" in macos_job
    assert 'test "$(uname -m)" = arm64' in macos_job
    assert '"$app/Contents/Resources/runtime/bin/python3"' in macos_job
    assert "AgentKitStudio-macos-arm64.zip" not in macos_job
    assert "Save verified DMG for manual validation" in macos_job


def test_windows_packager_generates_ico_before_downloading_runtime(tmp_path):
    """A clean CI checkout must create its icon before staging the Windows app."""
    from PIL import Image

    scripts = tmp_path / "scripts"
    scripts.mkdir()
    for name in ("package_studio_app_windows.sh", "create_studio_icon.py"):
        (scripts / name).write_bytes((ROOT / "scripts" / name).read_bytes())
    # The reviewed wheel validation is covered separately; stop this packaging
    # smoke before network/native runtimes and exercise the real icon generator.
    (scripts / "reviewed_web_assets.py").write_text("raise SystemExit(0)\n")
    (tmp_path / "dist").mkdir()
    (tmp_path / "dist/ksadk-0.8.7-py3-none-any.whl").write_bytes(b"fixture")
    commands = tmp_path / "commands"
    commands.mkdir()
    python = commands / "python3"
    python.write_text("#!/bin/sh\nexec " + shlex.quote(Path(sys.executable).as_posix()) + ' "$@"\n')
    python.chmod(0o755)
    curl = commands / "curl"
    curl.write_text('#!/bin/sh\ntest -s scripts/studio_icon.ico || exit 99\n'
                    'printf "icon-ready\n"\nexit 73\n')
    curl.chmod(0o755)
    result = subprocess.run(
        ["sh", "scripts/package_studio_app_windows.sh"], cwd=tmp_path,
        env={**os.environ, "PATH": f"{commands}{os.pathsep}{os.environ['PATH']}",
             "STUDIO_APP_DIR": str(tmp_path / "bundle"), "STUDIO_APP_VERSION": "0.8.7"},
        capture_output=True, text=True,
    )
    assert result.returncode == 73, result.stdout + result.stderr
    assert "icon-ready" in result.stdout
    with Image.open(scripts / "studio_icon.ico") as icon:
        assert icon.format == "ICO"
        assert {(16, 16), (32, 32), (256, 256)} <= icon.ico.sizes()

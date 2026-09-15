"""User-triggered desktop actions for verified, run-owned workspace documents.

The browser supplies a run and relative document reference, never an executable
or absolute filesystem path. Native actions belong to the computer running the
local Studio process; remote/proxied clients retain the download fallback.
"""

from __future__ import annotations

import ipaddress
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from fastapi import Request
from pydantic import BaseModel, ConfigDict, Field

from ksadk.studio.errors import StudioError
from ksadk.studio.run_documents import read_document


class NativeDocumentActionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1, max_length=1024)
    action: Literal["open", "reveal"]
    application_id: str | None = Field(default=None, alias="applicationId", max_length=64)


# Explicit desktop application allowlist. Do not accept program names, bundle
# identifiers, arguments or arbitrary application paths from browser input.
_MAC_APPLICATIONS = (
    ("vscode", "Visual Studio Code", "Visual Studio Code.app"),
    ("cursor", "Cursor", "Cursor.app"),
    ("typora", "Typora", "Typora.app"),
    ("markedit", "MarkEdit", "MarkEdit.app"),
    ("bbedit", "BBEdit", "BBEdit.app"),
    ("coteditor", "CotEditor", "CotEditor.app"),
    ("textedit", "TextEdit", "TextEdit.app"),
)


def _loopback(host: str | None) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host or "").is_loopback
    except ValueError:
        return False


def is_local_desktop_request(request: Request) -> bool:
    """Fail closed for remote peers/proxies even when the Host looks local."""
    if not request.client or not _loopback(request.client.host):
        return False
    if not _loopback(request.url.hostname):
        return False
    if any(
        key.lower() == "forwarded" or key.lower().startswith("x-forwarded-")
        for key in request.headers
    ):
        return False
    origin = request.headers.get("origin")
    if origin:
        parsed = urlparse(origin)
        if (parsed.scheme, parsed.netloc) != (request.url.scheme, request.url.netloc):
            return False
    return True


def _desktop_launcher() -> str | None:
    if sys.platform == "darwin":
        return "/usr/bin/open" if Path("/usr/bin/open").is_file() else None
    if sys.platform.startswith("linux") and (
        os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")
    ):
        return shutil.which("xdg-open")
    return None


def _application_roots() -> tuple[Path, ...]:
    return (Path("/Applications"), Path.home() / "Applications", Path("/System/Applications"))


def _installed_applications() -> dict[str, tuple[str, Path]]:
    if sys.platform != "darwin":
        return {}
    applications = {}
    for app_id, name, bundle in _MAC_APPLICATIONS:
        for root in _application_roots():
            path = root / bundle
            if path.is_dir():
                applications[app_id] = (name, path)
                break
    return applications


def document_metadata(studio, run_id: str, name: str, *, local: bool) -> dict:
    path, _ = read_document(studio, run_id, name)
    supported = local and _desktop_launcher() is not None
    applications = _installed_applications() if supported else {}
    return {
        "name": path.name,
        "path": str(path) if local else None,
        "relativePath": name,
        "platform": sys.platform,
        "capabilities": {
            "open": supported,
            "reveal": supported,
            "openWith": bool(applications),
        },
        "applications": [{"id": app_id, "name": app[0]} for app_id, app in applications.items()],
        "revealLabel": "在 Finder 中显示" if sys.platform == "darwin" else "打开所在文件夹",
    }


def _launch(command: list[str]) -> None:
    # No shell, no detached script and no raw browser-supplied arguments.
    subprocess.run(
        command,
        check=True,
        timeout=10,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def perform_document_action(
    studio, run_id: str, payload: NativeDocumentActionRequest, *, local: bool
) -> dict:
    if not local:
        raise StudioError(
            "NATIVE_DOCUMENT_LOCAL_ONLY",
            "仅本机直连 Studio 支持打开电脑中的文件；请下载后打开。",
            status_code=403,
        )
    path, _ = read_document(studio, run_id, payload.path)
    launcher = _desktop_launcher()
    if launcher is None:
        raise StudioError(
            "NATIVE_DOCUMENT_UNAVAILABLE",
            "当前 Studio 没有可用的桌面环境，请下载后打开。",
            status_code=409,
        )
    application = None
    if payload.application_id is not None:
        application = _installed_applications().get(payload.application_id)
        if payload.action != "open" or application is None:
            raise StudioError(
                "DOCUMENT_APPLICATION_UNAVAILABLE", "所选打开方式不可用。", status_code=422
            )
    try:
        if sys.platform == "darwin":
            command = [launcher]
            if payload.action == "reveal":
                command += ["-R"]
            elif application is not None:
                command += ["-a", str(application[1])]
            _launch([*command, str(path)])
        else:
            _launch([launcher, str(path if payload.action == "open" else path.parent)])
    except (OSError, subprocess.SubprocessError) as error:
        # Native launcher stderr may include unrelated local paths and details.
        raise StudioError(
            "DOCUMENT_OPEN_FAILED", "系统未能打开文件，请选择其他打开方式或下载。", status_code=502
        ) from error
    return {"status": "opened" if payload.action == "open" else "revealed", "name": path.name}

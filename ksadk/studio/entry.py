"""Studio entry selection and local-session bootstrap responses."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

from fastapi import Request
from fastapi.responses import RedirectResponse, Response
from ksadk.studio.errors import StudioError


async def studio_entry_response(
    studio: Any, request: Request, static_root: Path,
) -> Response:
    # Workspace navigation is contributed by the official Core client.
    # Opening the standalone React shell with enabled plugins silently
    # hides those pages. Select the host from Profile metadata, without
    # starting Core just to decide which entry to serve.
    use_core = False
    if request.url.path == "/":
        try:
            # 有界等待：探测可能触发 Node bridge 冷启动（可达数十秒），
            # 超时则先回 React shell，避免首屏长时间白屏；
            # 预热完成后真实 Core 用户下次导航仍会拿到正确重定向。
            use_core = await asyncio.wait_for(
                studio.dsh_capabilities.has_enabled_profile_plugins(), timeout=2.0
            )
        except (asyncio.TimeoutError, StudioError, OSError, RuntimeError):
            # The optional toolchain may be absent in a plain SDK workspace.
            pass
    if request.url.path == "/studio-recovery/":
        from ksadk.studio.recovery_page import RECOVERY_HTML

        response = Response(content=RECOVERY_HTML, media_type="text/html")
    # Desktop startup keeps the shell available while Core starts.
    elif use_core and os.environ.get("KSADK_STUDIO_LAZY_START") != "1":
        target = "/studio-core/"
        if request.url.query:
            target += "?" + request.url.query
        # Browsers inherit the original fragment across this redirect,
        # preserving Agent/session/group deep links and CLI bootstrap.
        response = RedirectResponse(target, status_code=307)
    else:
        path = static_root / "index.html"
        html = path.read_text(encoding="utf-8")
        # Keep hashed module URLs identical to internal lazy imports.
        response = Response(content=html, media_type="text/html")
    response.headers["Cache-Control"] = "no-store"
    return response

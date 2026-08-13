from __future__ import annotations

import socket
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator
from urllib.request import urlopen

import uvicorn

from ksadk.studio.api import create_studio_app


def write_skill(root: Path, name: str, body: str = "Follow the instructions.") -> Path:
    skill = root / name
    skill.mkdir(parents=True, exist_ok=True)
    (skill / "SKILL.md").write_text(
        "---\n"
        f"name: {name}\n"
        f"description: Exercise the {name} workflow.\n"
        "version: 1.0.0\n"
        "---\n\n"
        f"{body}\n",
        encoding="utf-8",
    )
    return skill


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@contextmanager
def studio_server(workspace: Path) -> Iterator[str]:
    port = free_port()
    app = create_studio_app(workspace, security_enabled=False)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{port}"
    try:
        for _ in range(100):
            try:
                with urlopen(f"{base_url}/api/v1/system/health", timeout=0.2) as response:
                    if response.status == 200:
                        break
            except OSError:
                time.sleep(0.05)
        else:
            raise AssertionError("Studio server did not start")
        yield base_url
    finally:
        server.should_exit = True
        thread.join(timeout=5)

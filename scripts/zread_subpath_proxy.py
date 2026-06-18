#!/usr/bin/env python3
"""Serve native zread under a non-root path.

zread's embedded UI currently assumes root-relative assets and APIs. This proxy
keeps zread itself unchanged, while making the app safe to publish below
`/ksadk-docs` on a shared ingress host.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
import gzip
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


LISTEN_HOST = os.environ.get("HOST", "0.0.0.0")
LISTEN_PORT = int(os.environ.get("PORT", "8080"))
UPSTREAM_HOST = "127.0.0.1"
UPSTREAM_PORT = int(os.environ.get("ZREAD_UPSTREAM_PORT", "9681"))
BASE_PATH = "/" + os.environ.get("DOCS_BASE_PATH", "/ksadk-docs").strip("/")
if BASE_PATH == "/":
    BASE_PATH = ""
try:
    CACHE_BUSTER = os.environ.get("DOCS_CACHE_BUSTER") or open(".zread/wiki/current", encoding="utf-8").read().strip()
except OSError:
    CACHE_BUSTER = str(int(time.time()))
CACHE_BUSTER = CACHE_BUSTER.replace("/", "-")


def start_zread() -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        [
            "zread",
            "browse",
            "--host",
            UPSTREAM_HOST,
            "--port",
            str(UPSTREAM_PORT),
            "--stdio",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.STDOUT,
    )


def wait_for_upstream(process: subprocess.Popen[bytes]) -> None:
    deadline = time.time() + 60
    url = f"http://{UPSTREAM_HOST}:{UPSTREAM_PORT}/"
    last_error: Exception | None = None
    while time.time() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"zread exited early with code {process.returncode}")
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                if response.status < 500:
                    return
        except Exception as exc:  # noqa: BLE001 - surfaced after retry window.
            last_error = exc
        time.sleep(0.5)
    raise RuntimeError(f"zread did not become ready: {last_error}")


def rewrite_text(body: bytes, content_type: str, content_encoding: str) -> bytes:
    if content_encoding.lower() == "gzip":
        body = gzip.decompress(body)

    if not any(token in content_type for token in ("text/html", "javascript", "text/css")):
        return body

    text = body.decode("utf-8", errors="replace")
    if "text/html" in content_type:
        text = text.replace('src="/', f'src="{BASE_PATH}/')
        text = text.replace('href="/', f'href="{BASE_PATH}/')
        text = text.replace('.js"', f'.js?v={CACHE_BUSTER}"')
        text = text.replace('.css"', f'.css?v={CACHE_BUSTER}"')
        if 'rel="icon"' not in text:
            text = text.replace("</head>", '<link rel="icon" href="data:,"></head>')

    if "javascript" in content_type:
        text = text.replace("`/api/", f"`{BASE_PATH}/api/")
        text = text.replace('"/api/', f'"{BASE_PATH}/api/')
        text = text.replace("'/api/", f"'{BASE_PATH}/api/")
        text = text.replace("fetch(`/api/", f"fetch(`{BASE_PATH}/api/")
        text = text.replace('fetch("/api/', f'fetch("{BASE_PATH}/api/')
        text = text.replace("href:`/api/", f"href:`{BASE_PATH}/api/")
        text = text.replace(
            "resolveInternalWikiHref:e=>`/${e}`",
            f"resolveInternalWikiHref:e=>`{BASE_PATH}/${{e}}`",
        )
        text = text.replace(
            "buildTopicHref:e=>`/${e}`",
            f"buildTopicHref:e=>`{BASE_PATH}/${{e}}`",
        )
        text = text.replace(
            "window.history.pushState(null,``,`/${e.slug}`)",
            f"window.history.pushState(null,``,`{BASE_PATH}/${{e.slug}}`)",
        )
        text = text.replace(
            "function pJt(){return window.location.pathname.replace(/^\\//,``)}",
            "function pJt(){return window.location.pathname"
            f".replace(/^\\/{BASE_PATH.strip('/')}\\/?/,``)"
            ".replace(/^\\//,``)}",
        )

    return text.encode("utf-8")


class ProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: object) -> None:
        sys.stderr.write("%s - - [%s] %s\n" % (self.address_string(), self.log_date_time_string(), fmt % args))

    def do_GET(self) -> None:  # noqa: N802 - stdlib hook name.
        self._proxy()

    def do_HEAD(self) -> None:  # noqa: N802 - stdlib hook name.
        self._proxy(head_only=True)

    def do_PUT(self) -> None:  # noqa: N802 - zread editor save endpoint.
        self._proxy(with_body=True)

    def _proxy(self, *, head_only: bool = False, with_body: bool = False) -> None:
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path == "/healthz":
            self._send_plain(200, "ok\n")
            return

        if BASE_PATH and parsed.path == BASE_PATH:
            self.send_response(308)
            self.send_header("Location", f"{BASE_PATH}/")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        upstream_path = parsed.path
        if BASE_PATH:
            if not (parsed.path == BASE_PATH or parsed.path.startswith(f"{BASE_PATH}/")):
                self._send_plain(404, "not found\n")
                return
            upstream_path = parsed.path[len(BASE_PATH) :] or "/"

        upstream_url = urllib.parse.urlunsplit(
            ("http", f"{UPSTREAM_HOST}:{UPSTREAM_PORT}", upstream_path, parsed.query, "")
        )
        body = None
        if with_body:
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length) if length else b""

        headers = {
            "Accept": self.headers.get("Accept", "*/*"),
            "Accept-Encoding": "identity",
        }
        if "Content-Type" in self.headers:
            headers["Content-Type"] = self.headers["Content-Type"]

        request = urllib.request.Request(upstream_url, data=body, headers=headers, method=self.command)
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                response_body = b"" if head_only else response.read()
                content_type = response.headers.get("Content-Type", "")
                content_encoding = response.headers.get("Content-Encoding", "")
                is_rewritten = any(token in content_type for token in ("text/html", "javascript", "text/css"))
                response_body = rewrite_text(response_body, content_type, content_encoding)
                self.send_response(response.status)
                for key, value in response.headers.items():
                    lower = key.lower()
                    if lower in {"content-length", "content-encoding", "transfer-encoding", "connection"}:
                        continue
                    if is_rewritten and lower == "cache-control":
                        continue
                    self.send_header(key, value)
                if is_rewritten:
                    self.send_header("Cache-Control", "no-cache")
                self.send_header("Content-Length", str(len(response_body)))
                self.end_headers()
                if not head_only:
                    self.wfile.write(response_body)
        except urllib.error.HTTPError as exc:
            error_body = b"" if head_only else exc.read()
            self.send_response(exc.code)
            self.send_header("Content-Length", str(len(error_body)))
            self.end_headers()
            if not head_only:
                self.wfile.write(error_body)

    def _send_plain(self, status: int, body: str) -> None:
        payload = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def main() -> int:
    process = start_zread()

    def stop_process(*_: object) -> None:
        if process.poll() is None:
            process.terminate()

    signal.signal(signal.SIGTERM, stop_process)
    signal.signal(signal.SIGINT, stop_process)

    wait_for_upstream(process)
    server = ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), ProxyHandler)

    watcher = threading.Thread(target=lambda: (process.wait(), server.shutdown()), daemon=True)
    watcher.start()

    try:
        server.serve_forever()
    finally:
        stop_process()
    return process.returncode or 0


if __name__ == "__main__":
    raise SystemExit(main())

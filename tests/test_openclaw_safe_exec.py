import os
import json
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory


REPO_ROOT = Path(__file__).resolve().parents[1]
SAFE_EXEC_SCRIPT = REPO_ROOT / "deploy" / "openclaw" / "safe-bin" / "openclaw-safe-exec"


def _run_sh_safe(workspace: Path, state_dir: Path, safe_bin_dir: Path, *args: str, extra_env: dict | None = None):
    env = os.environ.copy()
    env["OPENCLAW_SAFE_EXEC_COMMAND"] = "sh-safe"
    env["OPENCLAW_EXEC_SAFE_WORKSPACE_ROOT"] = str(workspace)
    env["OPENCLAW_EXEC_SAFE_STATE_DIR"] = str(state_dir)
    env["OPENCLAW_SAFE_BIN_DIR"] = str(safe_bin_dir)
    env["OPENCLAW_EXEC_STRICT_MODE"] = "true"
    if extra_env:
        env.update(extra_env)

    return subprocess.run(
        ["bash", str(SAFE_EXEC_SCRIPT), *args],
        cwd=str(workspace),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _run_bash_safe(workspace: Path, state_dir: Path, safe_bin_dir: Path, *args: str, extra_env: dict | None = None):
    env = os.environ.copy()
    env["OPENCLAW_SAFE_EXEC_COMMAND"] = "bash-safe"
    env["OPENCLAW_EXEC_SAFE_WORKSPACE_ROOT"] = str(workspace)
    env["OPENCLAW_EXEC_SAFE_STATE_DIR"] = str(state_dir)
    env["OPENCLAW_SAFE_BIN_DIR"] = str(safe_bin_dir)
    env["OPENCLAW_EXEC_STRICT_MODE"] = "true"
    if extra_env:
        env.update(extra_env)

    return subprocess.run(
        ["bash", str(SAFE_EXEC_SCRIPT), *args],
        cwd=str(workspace),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _run_web_safe(
    workspace: Path,
    state_dir: Path,
    safe_bin_dir: Path,
    *args: str,
    extra_env: dict | None = None,
):
    env = os.environ.copy()
    for key in (
        "OPENCLAW_WEB_SAFE_SEARCH_API_KEY",
        "OPENCLAW_MODEL_API_KEY",
        "OPENAI_API_KEY",
        "LLM_API_KEY",
        "MODEL_API_KEY",
        "OPENAI_MODEL_NAME",
        "OPENCLAW_DEFAULT_MODEL",
    ):
        env.pop(key, None)
    env["OPENCLAW_SAFE_EXEC_COMMAND"] = "web-safe"
    env["OPENCLAW_EXEC_SAFE_WORKSPACE_ROOT"] = str(workspace)
    env["OPENCLAW_EXEC_SAFE_STATE_DIR"] = str(state_dir)
    env["OPENCLAW_SAFE_BIN_DIR"] = str(safe_bin_dir)
    env["OPENCLAW_EXEC_STRICT_MODE"] = "true"
    if extra_env:
        env.update(extra_env)

    return subprocess.run(
        ["bash", str(SAFE_EXEC_SCRIPT), *args],
        cwd=str(workspace),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _run_mcporter_safe(
    workspace: Path,
    state_dir: Path,
    safe_bin_dir: Path,
    fake_bin_dir: Path,
    *args: str,
):
    env = os.environ.copy()
    env["OPENCLAW_SAFE_EXEC_COMMAND"] = "mcporter"
    env["OPENCLAW_EXEC_SAFE_WORKSPACE_ROOT"] = str(workspace)
    env["OPENCLAW_EXEC_SAFE_STATE_DIR"] = str(state_dir)
    env["OPENCLAW_SAFE_BIN_DIR"] = str(safe_bin_dir)
    env["OPENCLAW_EXEC_STRICT_MODE"] = "true"
    env["PATH"] = f"{fake_bin_dir}:{env['PATH']}"

    return subprocess.run(
        ["bash", str(SAFE_EXEC_SCRIPT), *args],
        cwd=str(workspace),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _start_http_server(handler_cls: type[BaseHTTPRequestHandler]) -> tuple[HTTPServer, threading.Thread]:
    server = HTTPServer(("127.0.0.1", 0), handler_cls)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def test_sh_safe_runs_workspace_script_with_scrubbed_env():
    with TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        workspace = root / "workspace"
        state_dir = root / "state"
        safe_bin_dir = root / "safe-bin"
        script_path = workspace / "env-check.sh"

        workspace.mkdir()
        state_dir.mkdir()
        safe_bin_dir.mkdir()
        script_path.write_text(
            "#!/bin/sh\n"
            "printf 'api=%s\\n' \"${OPENCLAW_MODEL_API_KEY:-missing}\"\n"
            "printf 'kdocs=%s\\n' \"${KDOCS_TOKEN:-missing}\"\n"
            "printf 'path=%s\\n' \"$PATH\"\n"
            "printf 'home=%s\\n' \"$HOME\"\n"
            "printf 'shell=%s\\n' \"$SHELL\"\n"
        )

        result = _run_sh_safe(
            workspace,
            state_dir,
            safe_bin_dir,
            str(script_path),
            extra_env={
                "OPENCLAW_MODEL_API_KEY": "top-secret",
                "KDOCS_TOKEN": "kdocs-secret",
            },
        )

        assert result.returncode == 0, result.stderr or result.stdout
        assert "api=missing" in result.stdout
        assert "kdocs=missing" in result.stdout
        assert f"path={safe_bin_dir.resolve()}" in result.stdout
        assert f"home={workspace.resolve()}" in result.stdout
        assert "shell=/bin/sh" in result.stdout or "shell=/usr/bin/sh" in result.stdout


def test_sh_safe_rejects_shell_options():
    with TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        workspace = root / "workspace"
        state_dir = root / "state"
        safe_bin_dir = root / "safe-bin"

        workspace.mkdir()
        state_dir.mkdir()
        safe_bin_dir.mkdir()

        result = _run_sh_safe(workspace, state_dir, safe_bin_dir, "-c", "echo hi")

        assert result.returncode == 126
        assert "does not accept shell options" in result.stderr


def test_sh_safe_rejects_scripts_outside_workspace():
    with TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        workspace = root / "workspace"
        state_dir = root / "state"
        safe_bin_dir = root / "safe-bin"
        outside_script = root / "outside.sh"

        workspace.mkdir()
        state_dir.mkdir()
        safe_bin_dir.mkdir()
        outside_script.write_text("#!/bin/sh\nexit 0\n")

        result = _run_sh_safe(workspace, state_dir, safe_bin_dir, str(outside_script))

        assert result.returncode == 126
        assert "path must remain inside workspace" in result.stderr


def test_bash_safe_runs_workspace_script_with_scrubbed_env():
    with TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        workspace = root / "workspace"
        state_dir = root / "state"
        safe_bin_dir = root / "safe-bin"
        script_path = workspace / "env-check.sh"

        workspace.mkdir()
        state_dir.mkdir()
        safe_bin_dir.mkdir()
        script_path.write_text(
            "#!/usr/bin/env bash\n"
            "printf 'api=%s\\n' \"${OPENCLAW_MODEL_API_KEY:-missing}\"\n"
            "printf 'home=%s\\n' \"$HOME\"\n"
            "printf 'shell=%s\\n' \"$SHELL\"\n"
        )

        result = _run_bash_safe(
            workspace,
            state_dir,
            safe_bin_dir,
            str(script_path),
            extra_env={"OPENCLAW_MODEL_API_KEY": "top-secret"},
        )

        assert result.returncode == 0, result.stderr or result.stdout
        assert "api=missing" in result.stdout
        assert f"home={workspace.resolve()}" in result.stdout
        assert "shell=/bin/bash" in result.stdout or "shell=/usr/bin/bash" in result.stdout


def test_bash_safe_rejects_shell_options():
    with TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        workspace = root / "workspace"
        state_dir = root / "state"
        safe_bin_dir = root / "safe-bin"

        workspace.mkdir()
        state_dir.mkdir()
        safe_bin_dir.mkdir()

        result = _run_bash_safe(workspace, state_dir, safe_bin_dir, "-c", "echo hi")

        assert result.returncode == 126
        assert "does not accept shell options" in result.stderr


def test_bash_safe_relaxed_mode_allows_shell_options_and_state_access():
    with TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        workspace = root / "workspace"
        state_dir = root / "state"
        safe_bin_dir = root / "safe-bin"
        marker_path = state_dir / "relaxed-ok.txt"

        workspace.mkdir()
        state_dir.mkdir()
        safe_bin_dir.mkdir()

        result = _run_bash_safe(
            workspace,
            state_dir,
            safe_bin_dir,
            "-lc",
            f"echo ok > {marker_path}",
            extra_env={"OPENCLAW_EXEC_STRICT_MODE": "false"},
        )

        assert result.returncode == 0, result.stderr or result.stdout
        assert marker_path.read_text().strip() == "ok"


def test_bash_safe_allows_allowlisted_state_kdocs_setup_script():
    with TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        workspace = root / "workspace"
        state_dir = root / "state"
        safe_bin_dir = root / "safe-bin"
        script_path = state_dir / "skills" / "kdocs" / "setup.sh"

        workspace.mkdir()
        state_dir.mkdir()
        safe_bin_dir.mkdir()
        script_path.parent.mkdir(parents=True, exist_ok=True)
        script_path.write_text(
            "#!/usr/bin/env bash\n"
            "printf 'home=%s\\n' \"$HOME\"\n"
            "if command -v grep >/dev/null 2>&1; then printf 'grep=ok\\n'; fi\n"
        )
        script_path.chmod(0o755)

        result = _run_bash_safe(workspace, state_dir, safe_bin_dir, str(script_path))

        assert result.returncode == 0, result.stderr or result.stdout
        assert f"home={state_dir.resolve()}" in result.stdout
        assert "grep=ok" in result.stdout


def test_bash_safe_rejects_non_allowlisted_state_scripts():
    with TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        workspace = root / "workspace"
        state_dir = root / "state"
        safe_bin_dir = root / "safe-bin"
        script_path = state_dir / "skills" / "other-skill" / "setup.sh"

        workspace.mkdir()
        state_dir.mkdir()
        safe_bin_dir.mkdir()
        script_path.parent.mkdir(parents=True, exist_ok=True)
        script_path.write_text("#!/usr/bin/env bash\nexit 0\n")
        script_path.chmod(0o755)

        result = _run_bash_safe(workspace, state_dir, safe_bin_dir, str(script_path))

        assert result.returncode == 126
        assert "state directory script is blocked unless allowlisted" in result.stderr


def test_web_safe_rejects_private_targets():
    with TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        workspace = root / "workspace"
        state_dir = root / "state"
        safe_bin_dir = root / "safe-bin"

        workspace.mkdir()
        state_dir.mkdir()
        safe_bin_dir.mkdir()

        result = _run_web_safe(
            workspace,
            state_dir,
            safe_bin_dir,
            "read",
            "http://127.0.0.1:8080/health",
        )

        assert result.returncode == 126
        assert "non-public address" in result.stderr or "host is blocked" in result.stderr


def test_web_safe_requires_query_for_search():
    with TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        workspace = root / "workspace"
        state_dir = root / "state"
        safe_bin_dir = root / "safe-bin"

        workspace.mkdir()
        state_dir.mkdir()
        safe_bin_dir.mkdir()

        result = _run_web_safe(workspace, state_dir, safe_bin_dir, "search")

        assert result.returncode == 2
        assert "search requires a query" in result.stderr


def test_web_safe_search_uses_bing_rss_by_default_even_when_model_provider_exists():
    with TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        workspace = root / "workspace"
        state_dir = root / "state"
        safe_bin_dir = root / "safe-bin"

        workspace.mkdir()
        state_dir.mkdir()
        safe_bin_dir.mkdir()
        (state_dir / "secrets.json").write_text(
            json.dumps({"providers": {"ksyun": {"apiKey": "search-secret"}}})
        )

        class ModelSearchHandler(BaseHTTPRequestHandler):
            requests: list[dict] = []

            def do_GET(self):
                ModelSearchHandler.requests.append(
                    {
                        "path": self.path,
                        "headers": dict(self.headers),
                    }
                )
                payload = (
                    "<?xml version='1.0' encoding='UTF-8'?>"
                    "<rss><channel>"
                    "<item>"
                    "<title>Default Result</title>"
                    "<link>https://example.com/default</link>"
                    "<description>default summary</description>"
                    "<pubDate>Tue, 18 Mar 2026 08:00:00 GMT</pubDate>"
                    "</item>"
                    "</channel></rss>"
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/rss+xml")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, format, *args):
                return

        server, thread = _start_http_server(ModelSearchHandler)
        try:
            result = _run_web_safe(
                workspace,
                state_dir,
                safe_bin_dir,
                "search",
                "OpenClaw",
                extra_env={
                    "OPENCLAW_MODEL_PROVIDER_ID": "ksyun",
                    "OPENCLAW_MODEL_BASE_URL": f"http://127.0.0.1:{server.server_port}/v1",
                    "OPENCLAW_MODEL_API_KEY_SECRET_SOURCE": "file",
                    "OPENCLAW_WEB_SAFE_SEARCH_ENDPOINT": f"http://127.0.0.1:{server.server_port}/search?q={{query}}",
                },
            )
        finally:
            server.shutdown()
            thread.join(timeout=5)

        assert result.returncode == 0, result.stderr or result.stdout
        assert "Source: Bing RSS (public web, no API key)" in result.stdout
        assert "https://example.com/default" in result.stdout
        assert len(ModelSearchHandler.requests) == 1
        assert ModelSearchHandler.requests[0]["path"].startswith("/search")


def test_web_safe_search_uses_model_search_only_when_explicitly_enabled():
    with TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        workspace = root / "workspace"
        state_dir = root / "state"
        safe_bin_dir = root / "safe-bin"

        workspace.mkdir()
        state_dir.mkdir()
        safe_bin_dir.mkdir()
        (state_dir / "secrets.json").write_text(
            json.dumps({"providers": {"ksyun": {"apiKey": "search-secret"}}})
        )

        class ModelSearchHandler(BaseHTTPRequestHandler):
            requests: list[dict] = []

            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length).decode("utf-8")
                ModelSearchHandler.requests.append(
                    {
                        "path": self.path,
                        "headers": dict(self.headers),
                        "body": json.loads(body),
                    }
                )
                payload = json.dumps(
                    {
                        "choices": [
                            {
                                "message": {
                                    "content": json.dumps(
                                        {
                                            "query": "OpenClaw",
                                            "results": [
                                                {
                                                    "title": "OpenClaw 文档",
                                                    "url": "https://example.com/openclaw",
                                                    "snippet": "官方文档入口",
                                                    "source": "Example",
                                                    "published_at": "2026-03-18",
                                                }
                                            ],
                                        }
                                    )
                                }
                            }
                        ]
                    }
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, format, *args):
                return

        server, thread = _start_http_server(ModelSearchHandler)
        try:
            result = _run_web_safe(
                workspace,
                state_dir,
                safe_bin_dir,
                "search",
                "OpenClaw",
                extra_env={
                    "OPENCLAW_WEB_SAFE_SEARCH_MODE": "model",
                    "OPENCLAW_MODEL_PROVIDER_ID": "ksyun",
                    "OPENCLAW_MODEL_BASE_URL": f"http://127.0.0.1:{server.server_port}/v1",
                    "OPENCLAW_MODEL_API_KEY_SECRET_SOURCE": "file",
                },
            )
        finally:
            server.shutdown()
            thread.join(timeout=5)

        assert result.returncode == 0, result.stderr or result.stdout
        assert "Source: KSPMAS web_search (model: deepseek-v3.2)" in result.stdout
        assert "https://example.com/openclaw" in result.stdout
        assert ModelSearchHandler.requests
        request = ModelSearchHandler.requests[0]
        assert request["path"] == "/v1/chat/completions"
        assert request["headers"]["Authorization"] == "Bearer search-secret"
        assert request["body"]["model"] == "deepseek-v3.2"
        assert request["body"]["response_format"] == {"type": "json_object"}
        assert request["body"]["tools"] == [{"type": "web_search"}]


def test_web_safe_search_falls_back_to_rss_when_model_search_fails():
    with TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        workspace = root / "workspace"
        state_dir = root / "state"
        safe_bin_dir = root / "safe-bin"

        workspace.mkdir()
        state_dir.mkdir()
        safe_bin_dir.mkdir()
        (state_dir / "secrets.json").write_text(
            json.dumps({"providers": {"ksyun": {"apiKey": "search-secret"}}})
        )

        class FallbackHandler(BaseHTTPRequestHandler):
            def do_POST(self):
                payload = b'{"error":{"message":"web_search unavailable"}}'
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def do_GET(self):
                payload = (
                    "<?xml version='1.0' encoding='UTF-8'?>"
                    "<rss><channel>"
                    "<item>"
                    "<title>Fallback Result</title>"
                    "<link>https://example.com/fallback</link>"
                    "<description>fallback summary</description>"
                    "<pubDate>Tue, 18 Mar 2026 08:00:00 GMT</pubDate>"
                    "</item>"
                    "</channel></rss>"
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/rss+xml")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, format, *args):
                return

        server, thread = _start_http_server(FallbackHandler)
        try:
            result = _run_web_safe(
                workspace,
                state_dir,
                safe_bin_dir,
                "search",
                "OpenClaw",
                extra_env={
                    "OPENCLAW_MODEL_PROVIDER_ID": "ksyun",
                    "OPENCLAW_MODEL_BASE_URL": f"http://127.0.0.1:{server.server_port}/v1",
                    "OPENCLAW_MODEL_API_KEY_SECRET_SOURCE": "file",
                    "OPENCLAW_WEB_SAFE_SEARCH_ENDPOINT": f"http://127.0.0.1:{server.server_port}/search?q={{query}}",
                },
            )
        finally:
            server.shutdown()
            thread.join(timeout=5)

        assert result.returncode == 0, result.stderr or result.stdout
        assert "Source: Bing RSS (public web, no API key)" in result.stdout
        assert "https://example.com/fallback" in result.stdout


def test_mcporter_allows_agent_reach_namespaces():
    with TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        workspace = root / "workspace"
        state_dir = root / "state"
        safe_bin_dir = root / "safe-bin"
        fake_bin_dir = root / "bin"
        fake_mcporter = fake_bin_dir / "mcporter"

        workspace.mkdir()
        state_dir.mkdir()
        safe_bin_dir.mkdir()
        fake_bin_dir.mkdir()
        fake_mcporter.write_text("#!/bin/sh\nprintf '%s\\n' \"$*\"\n")
        fake_mcporter.chmod(0o755)

        result = _run_mcporter_safe(
            workspace,
            state_dir,
            safe_bin_dir,
            fake_bin_dir,
            "call",
            'exa.web_search_exa(query: "OpenClaw", numResults: 1)',
        )

        assert result.returncode == 0, result.stderr or result.stdout
        assert 'exa.web_search_exa(query: "OpenClaw", numResults: 1)' in result.stdout


def test_mcporter_rejects_non_allowlisted_namespaces():
    with TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        workspace = root / "workspace"
        state_dir = root / "state"
        safe_bin_dir = root / "safe-bin"
        fake_bin_dir = root / "bin"
        fake_mcporter = fake_bin_dir / "mcporter"

        workspace.mkdir()
        state_dir.mkdir()
        safe_bin_dir.mkdir()
        fake_bin_dir.mkdir()
        fake_mcporter.write_text("#!/bin/sh\nprintf '%s\\n' \"$*\"\n")
        fake_mcporter.chmod(0o755)

        result = _run_mcporter_safe(
            workspace,
            state_dir,
            safe_bin_dir,
            fake_bin_dir,
            "call",
            'evil.do_thing()',
        )

        assert result.returncode == 126
        assert "restricted to built-in namespaces" in result.stderr

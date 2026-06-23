from __future__ import annotations

import base64
import asyncio
import importlib
import json
import os
import socket
import shutil
import subprocess
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import uvicorn
import websockets

from ksadk.runners.base_runner import BaseRunner
from ksadk.sessions.base import SessionEvent
from ksadk.sessions.in_memory import InMemorySessionService


class _E2ERunner(BaseRunner):
    def __init__(self):
        super().__init__(
            detection_result=SimpleNamespace(
                name="demo-agent",
                description="demo agent",
                type=SimpleNamespace(value="langgraph"),
            ),
            project_dir=".",
        )
        self.calls: list[dict] = []
        self.load_agent_calls = 0

    def load_agent(self) -> None:
        self.load_agent_calls += 1

    async def invoke(self, input_data: dict) -> dict:
        self.calls.append(input_data)
        return {"output": "assistant says hi"}

    async def stream(self, input_data: dict):
        self.calls.append(input_data)
        yield {"type": "final", "output": "assistant says hi"}


@contextmanager
def _run_real_http_server(app):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    host, port = sock.getsockname()
    sock.close()

    config = uvicorn.Config(app, host=host, port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    deadline = time.time() + 5
    while not server.started and time.time() < deadline:
        time.sleep(0.05)

    if not server.started:
        server.should_exit = True
        thread.join(timeout=5)
        raise RuntimeError("KsADK E2E server failed to start")

    try:
        yield f"http://{host}:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def _find_chromium_executable() -> str | None:
    explicit_path = os.environ.get("KSADK_E2E_CHROMIUM")
    if explicit_path and Path(explicit_path).is_file():
        return explicit_path

    candidates: list[Path] = []
    cache_roots = [
        Path.home() / "Library" / "Caches" / "ms-playwright",
        Path.home() / ".cache" / "ms-playwright",
    ]
    for cache_root in cache_roots:
        candidates.extend(
            cache_root.glob(
                "chromium-*/chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing"
            )
        )
        candidates.extend(
            cache_root.glob(
                "chromium-*/chrome-mac/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing"
            )
        )
        candidates.extend(cache_root.glob("chromium-*/chrome-linux/chrome"))

    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)

    for executable_name in (
        "chromium",
        "chromium-browser",
        "google-chrome",
        "google-chrome-stable",
        "chrome",
    ):
        resolved = shutil.which(executable_name)
        if resolved:
            return resolved
    return None


def _free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    _, port = sock.getsockname()
    sock.close()
    return int(port)


class _CdpBrowser:
    def __init__(self, executable_path: str):
        self.executable_path = executable_path
        self.port = _free_port()
        self._user_data_dir: tempfile.TemporaryDirectory[str] | None = None
        self._process: subprocess.Popen | None = None
        self._websocket = None
        self._next_id = 0

    async def __aenter__(self):
        self._user_data_dir = tempfile.TemporaryDirectory()
        self._process = subprocess.Popen(
            [
                self.executable_path,
                f"--remote-debugging-port={self.port}",
                f"--user-data-dir={self._user_data_dir.name}",
                "--headless=new",
                "--disable-background-networking",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--no-first-run",
                "--no-default-browser-check",
                "--no-sandbox",
                "about:blank",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        version_url = f"http://127.0.0.1:{self.port}/json/version"
        deadline = time.time() + 10
        version_payload: dict[str, str] | None = None
        async with httpx.AsyncClient(timeout=1, trust_env=False) as client:
            while time.time() < deadline:
                if self._process.poll() is not None:
                    raise RuntimeError("Chromium exited before DevTools was ready")
                try:
                    response = await client.get(version_url)
                    if response.status_code == 200:
                        version_payload = response.json()
                        break
                except Exception:
                    await asyncio.sleep(0.05)
        if not version_payload or not version_payload.get("webSocketDebuggerUrl"):
            raise RuntimeError("Chromium DevTools endpoint did not become ready")

        self._websocket = await websockets.connect(
            version_payload["webSocketDebuggerUrl"],
            max_size=None,
        )
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if self._websocket is not None:
            await self._websocket.close()
        if self._process is not None:
            self._process.terminate()
            try:
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait(timeout=5)
        if self._user_data_dir is not None:
            self._user_data_dir.cleanup()

    async def send(
        self,
        method: str,
        params: dict | None = None,
        *,
        session_id: str | None = None,
    ) -> dict:
        assert self._websocket is not None
        self._next_id += 1
        message: dict[str, object] = {"id": self._next_id, "method": method}
        if params is not None:
            message["params"] = params
        if session_id is not None:
            message["sessionId"] = session_id
        await self._websocket.send(json.dumps(message))

        while True:
            raw_message = await self._websocket.recv()
            payload = json.loads(raw_message)
            if payload.get("id") != self._next_id:
                continue
            if payload.get("error"):
                raise RuntimeError(f"CDP {method} failed: {payload['error']}")
            return payload.get("result") or {}

    async def new_page(self, url: str) -> "_CdpPage":
        target = await self.send("Target.createTarget", {"url": "about:blank"})
        attached = await self.send(
            "Target.attachToTarget",
            {"targetId": target["targetId"], "flatten": True},
        )
        page = _CdpPage(self, attached["sessionId"])
        await page.enable()
        await page.add_script_to_evaluate_on_new_document(
            """
            (() => {
              const originalFetch = window.fetch.bind(window);
              window.__ksadkE2E = { runAgentBodies: [] };
              window.fetch = async (...args) => {
                try {
                  const url = typeof args[0] === 'string' ? args[0] : args[0]?.url;
                  const init = args[1] || {};
                  if (String(url || '').includes('/agentengine/api/v1/RunAgent')) {
                    window.__ksadkE2E.runAgentBodies.push(JSON.parse(String(init.body || '{}')));
                  }
                } catch (error) {
                  window.__ksadkE2E.fetchPatchError = String(error);
                }
                return originalFetch(...args);
              };
            })();
            """
        )
        await page.navigate(url)
        return page


class _CdpPage:
    def __init__(self, browser: _CdpBrowser, session_id: str):
        self.browser = browser
        self.session_id = session_id

    async def enable(self) -> None:
        await self.browser.send("Page.enable", session_id=self.session_id)
        await self.browser.send("Runtime.enable", session_id=self.session_id)

    async def add_script_to_evaluate_on_new_document(self, source: str) -> None:
        await self.browser.send(
            "Page.addScriptToEvaluateOnNewDocument",
            {"source": source},
            session_id=self.session_id,
        )

    async def navigate(self, url: str) -> None:
        await self.browser.send("Page.navigate", {"url": url}, session_id=self.session_id)

    async def evaluate(self, expression: str, *, await_promise: bool = True):
        result = await self.browser.send(
            "Runtime.evaluate",
            {
                "expression": expression,
                "awaitPromise": await_promise,
                "returnByValue": True,
            },
            session_id=self.session_id,
        )
        if result.get("exceptionDetails"):
            raise AssertionError(result["exceptionDetails"])
        remote_object = result.get("result") or {}
        return remote_object.get("value")

    async def wait_for(self, expression: str, *, timeout: float = 10):
        deadline = time.time() + timeout
        while time.time() < deadline:
            value = await self.evaluate(expression)
            if value:
                return value
            await asyncio.sleep(0.1)
        raise AssertionError(f"Timed out waiting for browser expression: {expression}")


@pytest.fixture
def real_http_runtime(monkeypatch, tmp_path):
    server_app_module = importlib.import_module("ksadk.server.app")

    service = InMemorySessionService()
    runner = _E2ERunner()
    monkeypatch.setenv("AGENTENGINE_UI_DIR", str(tmp_path / ".agentengine" / "ui"))
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("OPENAI_API_BASE", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(server_app_module, "resolve_session_service", lambda: service)
    server_app_module.set_runner(runner)

    with _run_real_http_server(server_app_module.app) as base_url:
        yield base_url, runner, service


@pytest.mark.asyncio
async def test_real_http_responses_image_and_file_reach_runner_canonical_fields(
    real_http_runtime,
):
    base_url, runner, _ = real_http_runtime
    image_b64 = base64.b64encode(b"\x89PNG\r\n").decode("ascii")
    file_text = "候选人简历内容"
    file_b64 = base64.b64encode(file_text.encode("utf-8")).decode("ascii")

    async with httpx.AsyncClient(base_url=base_url, timeout=10, trust_env=False) as client:
        response = await client.post(
            "/v1/responses",
            json={
                "input": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "input_text", "text": "请分析图片和附件"},
                            {
                                "type": "input_image",
                                "image_url": f"data:image/png;base64,{image_b64}",
                            },
                            {
                                "type": "input_file",
                                "filename": "resume.txt",
                                "file_data": file_b64,
                            },
                        ],
                    }
                ],
                "stream": False,
            },
        )

    payload = response.json()
    assert response.status_code == 200
    assert payload["object"] == "response"
    assert payload["output_text"] == "assistant says hi"
    assert runner.calls[-1]["input_content"] == [
        {"type": "input_text", "text": "请分析图片和附件"},
        {"type": "input_image", "image_url": f"data:image/png;base64,{image_b64}"},
        {
            "type": "input_file",
            "filename": "resume.txt",
            "file_data": file_b64,
        },
    ]
    assert runner.calls[-1]["input_messages"] == [
        {
            "role": "user",
            "content": runner.calls[-1]["input_content"],
        }
    ]
    assert runner.calls[-1]["input_parts"][1] == {
        "inlineData": {
            "data": image_b64,
            "mimeType": "image/png",
            "displayName": "uploaded_image",
        }
    }
    assert runner.calls[-1]["current_attachments"][0]["mime_type"] == "image/png"
    assert runner.calls[-1]["current_attachment_results"][1]["text"] == file_text
    assert runner.calls[-1]["has_current_files"] is True


@pytest.mark.asyncio
async def test_real_http_responses_approval_resume_executes_builtin_tool(real_http_runtime):
    base_url, runner, service = real_http_runtime
    await service.create_session(agent_id="demo-agent", user_id="user", session_id="sess-e2e-approval")
    await service.append_event(
        "sess-e2e-approval",
        SessionEvent(
            author="demo-agent",
            event_type="approval_request",
            content={"role": "model", "parts": [{"text": "confirm write"}]},
            metadata={
                "interrupt_info": {
                    "approval_request_id": "appr_e2e",
                    "tool_name": "write_workspace_file",
                    "arguments": {"path": "e2e.txt", "content": "approved"},
                    "run_id": "call_e2e",
                    "server_label": "ksadk",
                }
            },
            invocation_id="inv-approval",
        ),
    )

    async with httpx.AsyncClient(base_url=base_url, timeout=10, trust_env=False) as client:
        response = await client.post(
            "/v1/responses",
            json={
                "session_id": "sess-e2e-approval",
                "input": [
                    {
                        "type": "mcp_approval_response",
                        "approval_request_id": "appr_e2e",
                        "approve": True,
                    }
                ],
                "stream": False,
            },
        )

    payload = response.json()
    assert response.status_code == 200
    assert payload["status"] == "completed"
    assert runner.calls[-1]["resume"] is True
    assert runner.calls[-1]["input"]["type"] == "function_call_output"
    assert runner.calls[-1]["input"]["call_id"] == "call_e2e"
    output = runner.calls[-1]["input"]["output"]
    assert output["ok"] is True
    assert Path(output["absolute_path"]).read_text(encoding="utf-8") == "approved"


@pytest.mark.asyncio
async def test_real_http_chat_completions_keeps_chat_response_and_converts_image_block(
    real_http_runtime,
):
    base_url, runner, _ = real_http_runtime
    image_url = "data:image/png;base64,aW1hZ2U="

    async with httpx.AsyncClient(base_url=base_url, timeout=10, trust_env=False) as client:
        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": "gpt-4o",
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "看图"},
                            {"type": "image_url", "image_url": {"url": image_url}},
                        ],
                    }
                ],
                "stream": False,
            },
        )

    payload = response.json()
    assert response.status_code == 200
    assert payload["object"] == "chat.completion"
    assert payload["choices"][0]["message"]["role"] == "assistant"
    assert runner.calls[-1]["input_content"] == [
        {"type": "input_text", "text": "看图"},
        {"type": "input_image", "image_url": image_url},
    ]
    assert runner.calls[-1]["input_messages"] == [
        {
            "role": "user",
            "content": [
                {"type": "input_text", "text": "看图"},
                {"type": "input_image", "image_url": image_url},
            ],
        }
    ]


@pytest.mark.asyncio
async def test_real_http_run_agent_uses_responses_input_and_uploaded_file_reference(
    real_http_runtime,
):
    base_url, runner, service = real_http_runtime
    attachment_bytes = "真实上传文件内容".encode("utf-8")

    async with httpx.AsyncClient(base_url=base_url, timeout=10, trust_env=False) as client:
        upload_response = await client.post(
            "/agentengine/api/v1/UploadFile",
            files={"file": ("report.txt", attachment_bytes, "text/plain")},
        )
        uploaded = upload_response.json()["Data"]["FileData"]
        response = await client.post(
            "/agentengine/api/v1/RunAgent",
            json={
                "AgentId": "demo-agent",
                "ApiFormat": "responses",
                "Messages": [{"role": "user", "content": "SHOULD_NOT_USE"}],
                "ResponsesInput": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "input_text", "text": "请总结上传文件"},
                            {
                                "type": "input_file",
                                "filename": uploaded["displayName"],
                                "file_url": uploaded["fileUri"],
                            },
                        ],
                    }
                ],
                "Stream": False,
            },
        )

    payload = response.json()
    assert upload_response.status_code == 200
    assert response.status_code == 200
    assert payload["Data"]["object"] == "response"
    assert "SHOULD_NOT_USE" not in runner.calls[-1]["input"]
    assert "真实上传文件内容" in runner.calls[-1]["input"]
    assert runner.calls[-1]["input_content"] == [
        {"type": "input_text", "text": "请总结上传文件"},
        {
            "type": "input_file",
            "filename": "report.txt",
            "file_url": uploaded["fileUri"],
        },
    ]
    assert runner.calls[-1]["current_attachments"][0]["file_uri"] == uploaded["fileUri"]
    assert runner.calls[-1]["current_attachment_results"][0]["text"] == "真实上传文件内容"

    session_id = payload["Data"]["session_id"]
    events = await service.get_events(session_id)
    assert events[0].content["parts"] == [
        {"type": "input_text", "text": "请总结上传文件"},
        {
            "type": "input_file",
            "filename": "report.txt",
            "file_url": uploaded["fileUri"],
        },
    ]
    assert events[0].metadata["attachments"] == [
        {
            "display_name": "report.txt",
            "file_uri": uploaded["fileUri"],
            "is_text": True,
            "mime_type": "text/plain",
            "size_bytes": len(attachment_bytes),
            "transport": "reference",
        }
    ]
    assert events[0].metadata["attachment_results"][0]["text_excerpt"] == "真实上传文件内容"


@pytest.mark.asyncio
async def test_real_http_run_agent_accepts_responses_input_without_legacy_messages(
    real_http_runtime,
):
    base_url, runner, _ = real_http_runtime
    responses_input = [
        {
            "role": "user",
            "content": [{"type": "input_text", "text": "只使用 ResponsesInput"}],
        }
    ]

    async with httpx.AsyncClient(base_url=base_url, timeout=10, trust_env=False) as client:
        response = await client.post(
            "/agentengine/api/v1/RunAgent",
            json={
                "AgentId": "demo-agent",
                "ApiFormat": "responses",
                "ResponsesInput": responses_input,
                "Stream": False,
            },
        )

    payload = response.json()
    assert response.status_code == 200
    assert payload["Data"]["object"] == "response"
    assert runner.calls[-1]["input_content"] == responses_input[0]["content"]
    assert runner.calls[-1]["input_messages"] == responses_input


@pytest.mark.asyncio
async def test_real_http_run_agent_uses_user_id_for_responses_runtime_trace(
    real_http_runtime,
):
    base_url, runner, service = real_http_runtime

    async with httpx.AsyncClient(base_url=base_url, timeout=10, trust_env=False) as client:
        response = await client.post(
            "/agentengine/api/v1/RunAgent",
            json={
                "AgentId": "demo-agent",
                "UserId": "ui-user-1",
                "ApiFormat": "responses",
                "Messages": [{"role": "user", "content": "hello"}],
                "ResponsesInput": [
                    {
                        "role": "user",
                        "content": [{"type": "input_text", "text": "hello"}],
                    }
                ],
                "Stream": False,
            },
        )

    payload = response.json()
    assert response.status_code == 200
    session_id = payload["Data"]["session_id"]
    session = await service.get_session(session_id)
    assert session is not None
    assert session.user_id == "ui-user-1"
    assert runner.calls[-1]["platform_context"]["user_id"] == "ui-user-1"


@pytest.mark.asyncio
async def test_real_http_static_ui_bundle_contains_responses_input_payload_builder(
    real_http_runtime,
):
    base_url, _, _ = real_http_runtime

    async with httpx.AsyncClient(base_url=base_url, timeout=10, trust_env=False) as client:
        root_response = await client.get("/")
        marker = 'src="./assets/'
        start = root_response.text.index(marker) + len('src=".')
        end = root_response.text.index('"', start)
        asset_path = root_response.text[start:end]
        asset_response = await client.get(asset_path)

    assert root_response.status_code == 200
    assert asset_response.status_code == 200
    assert "ResponsesInput" in asset_response.text
    assert "file_url" in asset_response.text


@pytest.mark.asyncio
async def test_real_browser_hosted_ui_file_upload_sends_responses_input_to_runner(
    real_http_runtime,
):
    chromium = _find_chromium_executable()
    if not chromium:
        pytest.skip("Chromium is required for the real Hosted UI browser E2E test")

    base_url, runner, _ = real_http_runtime

    async with _CdpBrowser(chromium) as browser:
        page = await browser.new_page(f"{base_url}/chat")
        await page.wait_for(
            """
            Boolean(
              document.querySelector('textarea') &&
              document.querySelector('input[type="file"]') &&
              document.querySelector('button[type="submit"]')
            )
            """
        )
        await page.evaluate(
            """
            (async () => {
              const fileInput = document.querySelector('input[type="file"]');
              const textarea = document.querySelector('textarea');
              const file = new File(
                [new TextEncoder().encode('真实浏览器上传内容')],
                'browser-report.txt',
                { type: 'text/plain' }
              );
              const transfer = new DataTransfer();
              transfer.items.add(file);
              Object.defineProperty(fileInput, 'files', {
                value: transfer.files,
                configurable: true,
              });
              fileInput.dispatchEvent(new Event('change', { bubbles: true }));
              await new Promise((resolve) => setTimeout(resolve, 100));

              const valueSetter = Object.getOwnPropertyDescriptor(
                HTMLTextAreaElement.prototype,
                'value'
              ).set;
              valueSetter.call(textarea, '请总结浏览器附件');
              textarea.dispatchEvent(new Event('input', { bubbles: true }));
              await new Promise((resolve) => setTimeout(resolve, 50));
              textarea.form.requestSubmit();
              return true;
            })()
            """
        )

        deadline = time.time() + 10
        while time.time() < deadline and not runner.calls:
            await asyncio.sleep(0.1)
        assert runner.calls, "Hosted UI did not reach RunAgent/runner"
        run_agent_bodies = await page.wait_for(
            "window.__ksadkE2E?.runAgentBodies?.length && window.__ksadkE2E.runAgentBodies",
        )

    run_agent_body = run_agent_bodies[0]
    assert run_agent_body["ApiFormat"] == "responses"
    assert run_agent_body["ResponsesInput"][0]["content"][0] == {
        "type": "input_text",
        "text": "请总结浏览器附件",
    }
    uploaded_part = run_agent_body["ResponsesInput"][0]["content"][1]
    assert uploaded_part["type"] == "input_file"
    assert uploaded_part["filename"] == "browser-report.txt"
    assert uploaded_part["file_url"].startswith("ksadk-upload://")
    assert run_agent_body["Messages"] == run_agent_body["ResponsesInput"]

    runner_payload = runner.calls[-1]
    assert runner_payload["input_content"] == run_agent_body["ResponsesInput"][0]["content"]
    assert runner_payload["current_attachments"][0]["display_name"] == "browser-report.txt"
    assert runner_payload["current_attachment_results"][0]["text"] == "真实浏览器上传内容"
    assert runner_payload["has_current_files"] is True


@pytest.mark.asyncio
async def test_real_browser_hosted_ui_image_upload_sends_input_image_to_runner(
    real_http_runtime,
):
    chromium = _find_chromium_executable()
    if not chromium:
        pytest.skip("Chromium is required for the real Hosted UI browser E2E test")

    base_url, runner, _ = real_http_runtime

    async with _CdpBrowser(chromium) as browser:
        page = await browser.new_page(f"{base_url}/chat")
        await page.wait_for(
            """
            Boolean(
              document.querySelector('textarea') &&
              document.querySelector('input[type="file"]') &&
              document.querySelector('button[type="submit"]')
            )
            """
        )
        await page.evaluate(
            """
            (async () => {
              const fileInput = document.querySelector('input[type="file"]');
              const textarea = document.querySelector('textarea');
              const file = new File(
                [new Uint8Array([0x89, 0x50, 0x4e, 0x47])],
                'browser-image.png',
                { type: 'image/png' }
              );
              const transfer = new DataTransfer();
              transfer.items.add(file);
              Object.defineProperty(fileInput, 'files', {
                value: transfer.files,
                configurable: true,
              });
              fileInput.dispatchEvent(new Event('change', { bubbles: true }));
              await new Promise((resolve) => setTimeout(resolve, 100));

              const valueSetter = Object.getOwnPropertyDescriptor(
                HTMLTextAreaElement.prototype,
                'value'
              ).set;
              valueSetter.call(textarea, '请看看这张图');
              textarea.dispatchEvent(new Event('input', { bubbles: true }));
              await new Promise((resolve) => setTimeout(resolve, 50));
              textarea.form.requestSubmit();
              return true;
            })()
            """
        )

        deadline = time.time() + 10
        while time.time() < deadline and not runner.calls:
            await asyncio.sleep(0.1)
        assert runner.calls, "Hosted UI did not reach RunAgent/runner"
        run_agent_bodies = await page.wait_for(
            "window.__ksadkE2E?.runAgentBodies?.length && window.__ksadkE2E.runAgentBodies",
        )

    run_agent_body = run_agent_bodies[0]
    assert run_agent_body["ApiFormat"] == "responses"
    assert run_agent_body["ResponsesInput"][0]["content"][0] == {
        "type": "input_text",
        "text": "请看看这张图",
    }
    image_part = run_agent_body["ResponsesInput"][0]["content"][1]
    assert image_part["type"] == "input_image"
    assert image_part["image_url"].startswith("data:image/png;base64,")
    assert "UploadFile" not in [
        body.get("Action")
        for body in run_agent_bodies
        if isinstance(body, dict)
    ]

    runner_payload = runner.calls[-1]
    assert runner_payload["input_content"] == run_agent_body["ResponsesInput"][0]["content"]
    assert runner_payload["current_attachments"][0]["display_name"] == "uploaded_image"
    assert runner_payload["current_attachments"][0]["mime_type"] == "image/png"
    assert runner_payload["has_current_files"] is True

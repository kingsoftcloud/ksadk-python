"""Browser acceptance for Scheduler Lite inside one Agent detail page.

The browser talks to the production FastAPI routes and React bundle.  Task
definitions and occurrence history use the real SQLite store; run-now crosses
the real AgentControl ingress, AgentKernel worker, Codex RuntimeAdapter and
Codex App Server.  Only the external model endpoint is replaced by a local
deterministic Responses server; it does not write Scheduler state or manufacture
a terminal occurrence.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from openai_codex import CodexConfig
from playwright.sync_api import Page, expect, sync_playwright
from studio_e2e_support import studio_server

from ksadk.codex.runtime import CodexRuntimeAdapter
from ksadk.events.canonical_store import RuntimeEventStore
from ksadk.runtime import RuntimeExecutor, RuntimeRegistry
from ksadk.studio.contracts import AgentSpec
from ksadk.studio.service import StudioService
from tests.e2e.codex_app_server_fixture import RealCodexFactory
from tests.e2e.codex_responses_stub import DeterministicResponsesStub

AGENT_ID = "scheduler-browser-agent"
AGENT_NAME = "Scheduler Browser Agent"
TASK_NAME = "工作日销售摘要"
TASK_PROMPT = "生成昨日销售摘要并列出异常"
TASK_NAME_EDITED = "工作日销售复盘"
TASK_PROMPT_EDITED = "生成昨日销售复盘并标注异常负责人"
CONTINUE_TASK_NAME = "持续销售跟进"
CONTINUE_SESSION_ID = "scheduler-codex-continuation"


def _runtime_inspector(_runtime: object) -> tuple[str, str, str]:
    return "0.8.2", "0.144.4", "codex-cli 0.144.4"


def _json(
    base_url: str,
    path: str,
    *,
    method: str = "GET",
    body: dict | None = None,
) -> dict:
    request = Request(
        f"{base_url}{path}",
        data=None if body is None else json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method=method,
    )
    with urlopen(request) as response:
        raw = response.read()
        assert response.status < 300, raw.decode("utf-8")
    return json.loads(raw) if raw else {}


def _agent_spec() -> AgentSpec:
    return AgentSpec.model_validate(
        {
            "description": "Scheduler browser fixture",
            "runtime": {"type": "codex", "version": "0.144.4"},
            "instructions": {
                "system": "You are a deterministic scheduler fixture.",
                "task": "Acknowledge scheduled work.",
            },
            "model": {
                "provider": "openai-compatible",
                "model": "fixture-model",
                "endpointUrl": "https://model.example.com/v1/chat/completions",
                "credentialRef": "env://MODEL_API_KEY",
                "parameters": {"temperature": 0.2, "maxTokens": 128},
            },
            "capabilities": {"skills": [], "mcpServers": [], "tools": []},
            "execution": {
                "strategy": "direct",
                "maxSteps": 4,
                "timeoutSeconds": 30,
                "retry": {"maxAttempts": 1, "backoffSeconds": 0},
            },
            "context": {
                "maxInputTokens": 4096,
                "reserveOutputTokens": 512,
                "compaction": {"enabled": True, "thresholdRatio": 0.8},
            },
            "security": {
                "toolPolicy": "deny-by-default",
                "allowedPermissions": [],
                "network": {
                    "mode": "restricted",
                    "allowedHosts": ["model.example.com"],
                    "allowPrivateNetwork": False,
                },
            },
        }
    )


def _prepare_agent(service: StudioService) -> str:
    spec = _agent_spec()
    service.create_studio_agent(
        agent_id=AGENT_ID,
        name=AGENT_NAME,
        description="Scheduler browser fixture",
        spec=spec,
        runtime=spec.runtime,
    )
    build = asyncio.run(service.ensure_current_build(AGENT_ID))
    return build.id


def _scheduled_instance_id(build_id: str) -> str:
    digest = hashlib.sha256(build_id.encode("utf-8")).hexdigest()[:24]
    return f"studio-schedule-{digest}"


def _start_browser_sse(page: Page, session_id: str, after_seq: int) -> None:
    page.evaluate(
        """
        ({sessionId, afterSeq}) => {
          window.__schedulerSseResult = (async () => {
            const controller = new AbortController();
            const timer = setTimeout(() => controller.abort(), 8000);
            try {
              const response = await fetch(
                `/api/v1/sessions/${encodeURIComponent(sessionId)}/events/stream?afterSeqId=${afterSeq}`,
                { signal: controller.signal, headers: { Accept: "text/event-stream" } },
              );
              if (!response.ok || !response.body) throw new Error(`SSE ${response.status}`);
              const reader = response.body.getReader();
              const decoder = new TextDecoder();
              let text = "";
              while (true) {
                const { value, done } = await reader.read();
                if (done) break;
                text += decoder.decode(value, { stream: true });
                if (text.includes('"type":"run.completed"')) {
                  controller.abort();
                  return { contentType: response.headers.get("content-type") || "", text };
                }
              }
              throw new Error("SSE ended without run.completed");
            } finally {
              clearTimeout(timer);
            }
          })();
        }
        """,
        {"sessionId": session_id, "afterSeq": after_seq},
    )


def _assert_scheduler_lifecycle(
    page: Page,
    base_url: str,
    *,
    build_id: str,
) -> tuple[str, str]:
    api_calls: list[tuple[str, str, int]] = []

    def record_schedule_response(response) -> None:
        path = urlsplit(response.url).path
        if "/schedules" in path:
            api_calls.append((response.request.method, path, response.status))

    page.on("response", record_schedule_response)
    # Create and edit from the global Scheduler product surface first. This is
    # intentionally a browser route, not repository setup hidden in the test.
    page.goto(f"{base_url}/#/automations", wait_until="domcontentloaded")
    expect(page.get_by_role("heading", name="自动化 / 定时任务")).to_be_visible()
    expect(page.get_by_text("本地调度运行中", exact=True)).to_be_visible()

    page.get_by_role("button", name="新建定时任务", exact=True).click()
    form = page.locator(".automation-form")
    expect(form).to_be_visible()
    form.locator("label", has_text="Agent").locator("select").select_option(AGENT_ID)
    form.get_by_placeholder("例如：工作日销售日报").fill(TASK_NAME)
    form.get_by_placeholder("例如：生成昨日销售摘要并列出异常").fill(TASK_PROMPT)
    form.locator("label", has_text="触发方式").locator("select").select_option("interval")
    form.locator("label", has_text="间隔（秒）").locator("input").fill("3600")
    form.get_by_role("button", name="创建任务", exact=True).click()

    row = page.get_by_role("row", name=f"查看定时任务 {TASK_NAME} 的详情")
    expect(row).to_be_visible()
    task_payload = _json(base_url, "/api/v1/schedules")
    assert len(task_payload["items"]) == 1
    task = task_payload["items"][0]
    task_id = task["taskId"]
    assert task["target"]["agentId"] == AGENT_ID
    assert task["target"]["agentInstanceId"] == _scheduled_instance_id(build_id)
    assert task["target"]["agentVersionRef"] == build_id

    # The global detail owns real edit, not a disabled or decorative action.
    row.click()
    page.locator(".automation-detail").get_by_role("button", name="编辑", exact=True).click()
    form.get_by_placeholder("例如：工作日销售日报").fill(TASK_NAME_EDITED)
    form.get_by_placeholder("例如：生成昨日销售摘要并列出异常").fill(TASK_PROMPT_EDITED)
    form.get_by_role("button", name="保存变更", exact=True).click()
    row = page.get_by_role("row", name=f"查看定时任务 {TASK_NAME_EDITED} 的详情")
    expect(row).to_be_visible()
    edited = _json(base_url, "/api/v1/schedules")["items"][0]
    assert edited["displayName"] == TASK_NAME_EDITED
    assert edited["command"]["payload"]["content"] == TASK_PROMPT_EDITED

    # A browser refresh reconstructs the global route and durable SQLite task
    # rather than depending on React component state.
    page.reload(wait_until="domcontentloaded")
    row = page.get_by_role("row", name=f"查看定时任务 {TASK_NAME_EDITED} 的详情")
    expect(row).to_be_visible()

    # The same durable task is managed in place from the Agent detail tab.
    page.goto(f"{base_url}/#/agents/{AGENT_ID}", wait_until="domcontentloaded")
    expect(page.get_by_role("banner", name="当前页面")).to_contain_text(AGENT_NAME)
    page.get_by_role("tab", name="自动化", exact=True).click()
    expect(page.get_by_role("heading", name="该 Agent 的自动化")).to_be_visible()
    row = page.get_by_role("row", name=f"查看定时任务 {TASK_NAME_EDITED} 的详情")
    expect(row).to_be_visible()

    # Activating the real table row opens task details and occurrence history.
    row.click()
    expect(
        page.locator(".automation-detail").get_by_role("heading", name=TASK_NAME_EDITED)
    ).to_be_visible()
    expect(page.locator(".automation-detail")).to_contain_text("目标 Build")
    expect(page.get_by_text("还没有执行记录。", exact=True)).to_be_visible()

    # Enabled state is durable and both transitions use the agent-scoped PUT.
    page.get_by_role("button", name="停用", exact=True).click()
    expect(
        page.locator(".automation-detail").get_by_role("button", name="启用", exact=True)
    ).to_be_visible()
    assert _json(base_url, "/api/v1/schedules")["items"][0]["enabled"] is False
    page.locator(".automation-detail").get_by_role("button", name="启用", exact=True).click()
    expect(
        page.locator(".automation-detail").get_by_role("button", name="停用", exact=True)
    ).to_be_visible()
    assert _json(base_url, "/api/v1/schedules")["items"][0]["enabled"] is True

    # Manual execution is accepted by AgentControl first. The real Kernel
    # worker and Codex RuntimeAdapter emit the correlated canonical terminal;
    # accepted alone is never treated as success.
    page.get_by_role("button", name="立即运行", exact=True).click()
    expect(page.get_by_text("已提交到本地 Agent Kernel", exact=True)).to_be_visible()

    terminal = None
    for _ in range(100):
        values = _json(
            base_url,
            f"/api/v1/agents/{AGENT_ID}/schedules/{task_id}/occurrences",
        )["items"]
        if values and values[0]["state"] in {"succeeded", "failed", "cancelled", "skipped"}:
            terminal = values[0]
            break
        time.sleep(0.05)
    assert terminal is not None, "real AgentKernel execution never produced a terminal occurrence"
    assert terminal["state"] == "succeeded", terminal
    # The product surface keeps polling active occurrences until the same
    # accepted row reaches a correlated terminal; users do not need a refresh.
    expect(page.locator(".automation-occurrences")).to_contain_text("成功", timeout=5000)

    # Refresh after execution proves that terminal history is persisted and
    # reloaded from HTTP, not retained in the component tree.
    page.reload(wait_until="domcontentloaded")
    page.get_by_role("tab", name="自动化", exact=True).click()
    row = page.get_by_role("row", name=f"查看定时任务 {TASK_NAME_EDITED} 的详情")
    row.click()
    occurrence_list = page.locator(".automation-occurrences")
    expect(occurrence_list).to_be_visible()
    expect(occurrence_list).to_contain_text("成功")
    expect(occurrence_list).to_contain_text("手动触发")

    occurrences = _json(
        base_url,
        f"/api/v1/agents/{AGENT_ID}/schedules/{task_id}/occurrences",
    )["items"]
    assert len(occurrences) == 1
    assert occurrences[0]["state"] == "succeeded"
    assert occurrences[0]["trigger"] == "manual"
    assert occurrences[0]["runId"], occurrences[0]

    page.get_by_role("button", name="删除", exact=True).click()
    dialog = page.get_by_role("alertdialog", name=f"删除定时任务「{TASK_NAME_EDITED}」？")
    expect(dialog).to_be_visible()
    dialog.get_by_role("button", name="删除任务", exact=True).click()
    expect(page.get_by_text("该 Agent 还没有定时任务", exact=True)).to_be_visible()
    assert _json(base_url, "/api/v1/schedules")["items"] == []
    # Deleting a definition does not erase its audit history.
    retained = _json(base_url, f"/api/v1/schedules/{task_id}/occurrences")["items"]
    assert [item["state"] for item in retained] == ["succeeded"]

    # Codex follow-up is also a browser path. The second occurrence must resume
    # the native thread and its terminal must cross the real SessionEvent SSE.
    page.get_by_role("button", name="新建任务", exact=True).click()
    form = page.locator(".automation-form")
    expect(form).to_be_visible()
    form.locator("label", has_text="Agent").locator("select").select_option(AGENT_ID)
    form.get_by_placeholder("例如：工作日销售日报").fill(CONTINUE_TASK_NAME)
    form.get_by_placeholder("例如：生成昨日销售摘要并列出异常").fill("继续生成销售跟进摘要")
    form.locator("label", has_text="触发方式").locator("select").select_option("interval")
    form.locator("label", has_text="间隔（秒）").locator("input").fill("3600")
    form.locator("label", has_text="会话").locator("select").select_option("continue_session")
    form.get_by_placeholder("选择或粘贴可恢复的本地 Session").fill(CONTINUE_SESSION_ID)
    form.get_by_role("button", name="创建任务", exact=True).click()
    continue_row = page.get_by_role("row", name=f"查看定时任务 {CONTINUE_TASK_NAME} 的详情")
    expect(continue_row).to_be_visible()
    continue_task = _json(base_url, "/api/v1/schedules")["items"][0]
    continue_task_id = continue_task["taskId"]
    continue_row.click()

    page.locator(".automation-detail").get_by_role("button", name="立即运行", exact=True).click()
    first_followup = None
    for _ in range(100):
        values = _json(
            base_url,
            f"/api/v1/schedules/{continue_task_id}/occurrences",
        )["items"]
        if len(values) == 1 and values[0]["state"] in {
            "succeeded",
            "failed",
            "cancelled",
            "skipped",
        }:
            first_followup = values[0]
            break
        time.sleep(0.05)
    assert first_followup is not None
    assert first_followup["state"] == "succeeded", first_followup

    replay = _json(
        base_url,
        f"/api/v1/sessions/{CONTINUE_SESSION_ID}/events?limit=500",
    )
    after_seq = replay["page"]["latestSeqId"]
    page.reload(wait_until="domcontentloaded")
    page.get_by_role("tab", name="自动化", exact=True).click()
    page.get_by_role("row", name=f"查看定时任务 {CONTINUE_TASK_NAME} 的详情").click()
    _start_browser_sse(page, CONTINUE_SESSION_ID, after_seq)
    page.locator(".automation-detail").get_by_role("button", name="立即运行", exact=True).click()

    continued = None
    for _ in range(100):
        values = _json(
            base_url,
            f"/api/v1/schedules/{continue_task_id}/occurrences",
        )["items"]
        if len(values) == 2 and all(
            item["state"] in {"succeeded", "failed", "cancelled", "skipped"} for item in values
        ):
            continued = values
            break
        time.sleep(0.05)
    assert continued is not None
    assert [item["state"] for item in continued] == ["succeeded", "succeeded"]
    assert {item["sessionId"] for item in continued} == {CONTINUE_SESSION_ID}
    assert len({item["runId"] for item in continued}) == 2
    sse = page.evaluate("() => window.__schedulerSseResult")
    assert sse["contentType"].startswith("text/event-stream"), sse
    assert '"type":"run.completed"' in sse["text"], sse

    assert api_calls.count(("POST", f"/api/v1/agents/{AGENT_ID}/schedules", 201)) == 2
    assert api_calls.count(("PUT", f"/api/v1/agents/{AGENT_ID}/schedules/{task_id}", 200)) == 3
    assert ("POST", f"/api/v1/agents/{AGENT_ID}/schedules/{task_id}:run", 202) in api_calls
    assert ("DELETE", f"/api/v1/agents/{AGENT_ID}/schedules/{task_id}", 204) in api_calls
    assert (
        api_calls.count(
            ("POST", f"/api/v1/agents/{AGENT_ID}/schedules/{continue_task_id}:run", 202)
        )
        == 2
    )
    return CONTINUE_SESSION_ID, continued[0]["runId"]


def main() -> None:
    with TemporaryDirectory(prefix="ksadk-scheduler-browser-") as temp_dir:
        workspace = Path(temp_dir)
        with DeterministicResponsesStub() as responses:
            client_factory = RealCodexFactory(responses_url=responses.base_url)
            codex_config = CodexConfig(env={"CODEX_HOME": str(workspace / "codex-home")})
            registry = RuntimeRegistry()
            registry.register(
                "codex",
                lambda _context: CodexRuntimeAdapter(client_factory(codex_config)),
            )
            service = StudioService(
                workspace,
                codex_runtime_inspector=_runtime_inspector,
                runtime_executor=RuntimeExecutor(registry),
            )
            build_id = _prepare_agent(service)

            with (
                studio_server(workspace, service=service) as base_url,
                sync_playwright() as playwright,
            ):
                browser = playwright.chromium.launch(headless=True)
                try:
                    page = browser.new_page(viewport={"width": 1440, "height": 960})
                    page_errors: list[str] = []
                    page.on("pageerror", lambda error: page_errors.append(str(error)))
                    session_id, run_id = _assert_scheduler_lifecycle(
                        page,
                        base_url,
                        build_id=build_id,
                    )
                    assert page_errors == [], f"Uncaught React page errors: {page_errors}"
                finally:
                    browser.close()

            requests = responses.requests()
            assert len(requests) == 3, requests
            native_thread_ids = [
                request.payload["client_metadata"]["thread_id"] for request in requests
            ]
            assert native_thread_ids[0] != native_thread_ids[1]
            assert native_thread_ids[1] == native_thread_ids[2]
            assert len(client_factory.processes) == 3
            assert all(process.poll() is not None for process in client_factory.processes)

        events = asyncio.run(
            RuntimeEventStore(service.session_service).list(session_id, run_id=run_id)
        )
        assert events[-1].event_type == "run.completed"


if __name__ == "__main__":
    main()

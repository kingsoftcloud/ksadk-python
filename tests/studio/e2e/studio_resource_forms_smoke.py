"""Exercise resource forms and Agent editing against an isolated local Studio.

Build React first and run with ``PYTHONPATH=. uv run python``. Model/MCP probe requests are
intercepted before they reach the server. All credentials are fixture strings;
resource creation and Agent updates use the real temporary-workspace backend.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import unquote, urlparse

from conversation_items_browser_e2e import (
    CanonicalConversationEvents,
    RuntimeFixture,
    _runtime_inspector,
    _seed_agent,
)
from playwright.sync_api import Page, expect, sync_playwright
from studio_e2e_support import studio_server

from ksadk.studio.service import StudioService


def visible_control(page: Page, control) -> dict:
    box = control.bounding_box()
    assert box, "Missing control"
    viewport = page.viewport_size
    assert viewport
    assert box["x"] >= -1 and box["y"] >= -1, box
    assert box["x"] + box["width"] <= viewport["width"] + 1, box
    assert box["y"] + box["height"] <= viewport["height"] + 1, box
    assert control.evaluate(
        "e => { const r=e.getBoundingClientRect();"
        "const hit=document.elementFromPoint(r.x+r.width/2,r.y+r.height/2);"
        "return !!hit && (e===hit || e.contains(hit)); }"
    ), "Control is covered"
    # Toasts intentionally ignore pointer events. Temporarily include them in
    # hit testing so a painted toast cannot visually hide an otherwise clickable
    # dialog action; restore every inline style before returning.
    assert control.evaluate(
        """e => {
          const toasts = [...document.querySelectorAll('.toast')];
          const previous = toasts.map(toast => toast.style.pointerEvents);
          try {
            toasts.forEach(toast => { toast.style.pointerEvents = 'auto'; });
            const r = e.getBoundingClientRect();
            const hit = document.elementFromPoint(r.x+r.width/2, r.y+r.height/2);
            return !!hit && (hit === e || e.contains(hit));
          } finally {
            toasts.forEach((toast, index) => { toast.style.pointerEvents = previous[index]; });
          }
        }"""
    ), "Control is visually hidden behind a toast"
    return box


def run(output: Path | None = None) -> list[dict]:
    results = []
    if output:
        output.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="studio-resource-forms-") as temporary:
        workspace = Path(temporary)
        tool_file = workspace / "layout_tool.py"
        tool_file.write_text(
            "def sample_tool(value: str) -> str:\n"
            '    """Return the input unchanged."""\n'
            "    return value\n"
        )
        runtime = RuntimeFixture(CanonicalConversationEvents())
        service = StudioService(
            workspace, codex_runtime_inspector=_runtime_inspector, runtime_executor=runtime.executor
        )
        _seed_agent(service)
        with studio_server(workspace, service=service) as base, sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            for width in [1440, 390, 360]:
                context = browser.new_context(viewport={"width": width, "height": 900})
                context.add_init_script("localStorage.setItem('agentkit-studio-theme', 'light')")
                requests: list[dict] = []
                mocked: list[str] = []
                external: list[str] = []

                def route_request(route):
                    request = route.request
                    path = unquote(urlparse(request.url).path)
                    if not request.url.startswith(base + "/"):
                        external.append(request.url)
                        route.abort()
                        return
                    if request.method in {"PUT", "POST", "DELETE"}:
                        payload = None
                        if "application/json" in request.headers.get("content-type", ""):
                            payload = request.post_data_json
                        requests.append({"path": path, "method": request.method, "body": payload})
                    if path == "/api/v1/model-endpoints:probe":
                        mocked.append(path)
                        route.fulfill(
                            json={
                                "recommended": {
                                    "wireApi": "chat",
                                    "endpointUrl": "https://model.example.com/v1/chat/completions",
                                    "status": "ok",
                                },
                                "attempts": [{"protocol": "chat", "status": "ok", "latencyMs": 1}],
                                "models": ["layout-model"],
                            }
                        )
                    elif path.startswith("/api/v1/model-profiles/") and path.endswith(":test"):
                        mocked.append(path)
                        route.fulfill(json={"ok": True, "latencyMs": 1})
                    elif path.startswith("/api/v1/catalog/mcp-servers/") and path.endswith(
                        ":probe"
                    ):
                        mocked.append(path)
                        route.fulfill(json={"status": "ready", "health": {"toolCount": 1}})
                    elif path.startswith("/api/v1/plugin-ecosystems/"):
                        route.fulfill(json={"items": []})
                    else:
                        route.continue_()

                context.route("**/*", route_request)
                page = context.new_page()
                page_errors = []
                page.on("pageerror", lambda error: page_errors.append(str(error)))
                slug = f"layout-model-{width}"
                model_id = f"fixture-model-{width}"
                model_label = f"布局模型 {width}"
                credential = f"LAYOUT_MODEL_{width}"

                def capture(name, **details):
                    if output:
                        page.screenshot(path=str(output / f"{width}-{name}.png"))
                    results.append(
                        {"width": width, "scenario": name, "status": "passed", **details}
                    )
                    print(width, name, "passed", flush=True)

                def navigate(route):
                    page.goto(base + "/#/" + route)
                    expect(page.locator(".app-shell")).to_be_visible()

                def open_resource(kind, label):
                    navigate("resources/" + kind)
                    expect(page.locator(".studio-data-table")).not_to_have_attribute(
                        "data-state", "loading"
                    )
                    page.get_by_role("button", name=label, exact=True).click()
                    expect(page.get_by_role("dialog")).to_be_visible()

                def form_frame(primary):
                    dialog = page.get_by_role("dialog")
                    dialog.evaluate(
                        "async e => { await Promise.all(e.getAnimations()"
                        ".map(animation => animation.finished.catch(() => {}))); }"
                    )
                    for control in [
                        dialog.get_by_role("button", name="关闭", exact=True),
                        *dialog.locator(".drawer-footer button").all(),
                    ]:
                        visible_control(page, control)
                    expect(dialog.get_by_role("button", name=primary, exact=True)).to_be_enabled()

                def last(path, method="POST"):
                    return next(
                        item
                        for item in reversed(requests)
                        if item["path"] == path and item["method"] == method
                    )

                def paste_mcp(configuration):
                    # Exercise the paste handler without changing the user's
                    # system clipboard from a headless browser.
                    field = page.locator("#mcpPaste")
                    field.fill(configuration)
                    field.dispatch_event("paste")

                def reopen_mcp(name, transport, endpoint):
                    row = page.locator("tbody tr").filter(has_text=name)
                    expect(row).to_be_visible()
                    row.get_by_role("button", name="查看", exact=True).click()
                    dialog = page.get_by_role("dialog", name=name, exact=True)
                    expect(dialog).to_be_visible()
                    expect(dialog.locator("dl > div").filter(has_text="Transport")).to_contain_text(
                        transport
                    )
                    expect(dialog.locator("dl > div").filter(has_text="Endpoint")).to_contain_text(
                        endpoint
                    )
                    dialog.evaluate(
                        "async e => { await Promise.all(e.getAnimations()"
                        ".map(animation => animation.finished.catch(() => {}))); }"
                    )
                    visible_control(page, dialog.get_by_role("button", name="关闭", exact=True))
                    capture(f"mcp-{transport}-reopened", actualLocalApi=True)
                    dialog.get_by_role("button", name="关闭", exact=True).click()

                try:
                    open_resource("model", "配置模型")
                    form_frame("创建模型")
                    count = len(requests)
                    page.get_by_role("button", name="创建模型", exact=True).click()
                    expect(page.locator("#amName")).to_have_attribute("aria-invalid", "true")
                    assert len(requests) == count
                    capture("model-validation")
                    page.locator("#amName").fill("discarded-model")
                    page.get_by_role("button", name="取消", exact=True).click()
                    assert len(requests) == count
                    page.get_by_role("button", name="配置模型", exact=True).click()
                    expect(page.locator("#amName")).to_have_value("")
                    for field, value in {
                        "amName": slug,
                        "amDisplayName": model_label,
                        "amModelId": model_id,
                        "amEndpoint": "https://model.example.com/v1/chat/completions",
                        "amEnvName": credential,
                        "amApiKey": "fixture-model-value",
                        "amTemp": "0.4",
                        "amMaxTokens": "1024",
                    }.items():
                        page.locator("#" + field).fill(value)
                    page.get_by_role("button", name="智能探测", exact=True).click()
                    expect(page.get_by_text("Chat · 可用 1ms", exact=True)).to_be_visible()
                    form_frame("创建模型")
                    capture("model-filled")
                    page.get_by_role("button", name="创建模型", exact=True).click()
                    expect(page.get_by_role("dialog")).not_to_be_visible()
                    spec = last("/api/v1/catalog/model-profiles")["body"]
                    assert spec["name"] == slug and spec["displayName"] == model_label
                    assert spec["spec"]["model"] == model_id
                    assert spec["spec"]["parameters"] == {"temperature": 0.4, "max_tokens": 1024}
                    assert spec["spec"]["credentialRef"] == f"env://{credential}"
                    assert last(f"/api/v1/credentials/{credential}", "PUT")["body"] == {
                        "value": "fixture-model-value",
                        "persistence": "session",
                    }
                    row = page.locator("tbody tr").filter(has_text=model_label)
                    expect(row).to_be_visible()
                    row.get_by_role("button", name="配置凭证", exact=True).click()
                    form_frame("保存并测试")
                    count = len(requests)
                    page.locator("input[type=password]").fill("fixture-discarded-value")
                    page.get_by_role("button", name="取消", exact=True).click()
                    assert len(requests) == count
                    row.get_by_role("button", name="配置凭证", exact=True).click()
                    form_frame("保存并测试")
                    expect(page.locator("input[type=password]")).to_have_value("")
                    visible_control(page, page.get_by_role("button", name="仅保存", exact=True))
                    page.locator("input[type=password]").fill("fixture-replaced-value")
                    page.get_by_role("button", name="仅保存", exact=True).click()
                    expect(page.get_by_role("dialog")).not_to_be_visible()
                    assert (
                        last(f"/api/v1/credentials/{credential}", "PUT")["body"]["value"]
                        == "fixture-replaced-value"
                    )
                    row.get_by_role("button", name="配置凭证", exact=True).click()
                    form_frame("保存并测试")
                    capture("model-credential")
                    page.get_by_role("button", name="保存并测试", exact=True).click()
                    expect(page.get_by_role("dialog")).not_to_be_visible()
                    row.get_by_role("button", name="配置凭证", exact=True).click()
                    form_frame("保存并测试")
                    page.get_by_role("button", name="清除已保存凭证", exact=True).click()
                    expect(page.get_by_role("dialog")).not_to_be_visible()
                    assert last(f"/api/v1/credentials/{credential}", "DELETE")
                    row.get_by_role("button", name="配置凭证", exact=True).click()
                    form_frame("保存并测试")
                    count = len(requests)
                    page.get_by_role("button", name="仅保存", exact=True).click()
                    expect(page.locator("input[type=password]")).to_have_attribute(
                        "aria-invalid", "true"
                    )
                    assert len(requests) == count
                    page.locator("input[type=password]").fill("fixture-restored-value")
                    page.get_by_role("button", name="保存并测试", exact=True).click()
                    expect(page.get_by_role("dialog")).not_to_be_visible()
                    capture("model-saved", actualLocalApi=True, externalProbe="mocked")

                    open_resource("tool", "添加 Python Tool")
                    form_frame("保存 Tool")
                    count = len(requests)
                    page.get_by_role("button", name="保存 Tool", exact=True).click()
                    expect(page.locator("#ptDisplayName")).to_have_attribute("aria-invalid", "true")
                    assert len(requests) == count
                    page.get_by_role("button", name="工作区路径", exact=True).click()
                    for field, value in {
                        "ptSource": "layout_tool.py",
                        "ptDisplayName": f"工作区工具 {width}",
                        "ptName": f"layout_tool_{width}",
                        "ptCallable": "sample_tool",
                        "ptDesc": "布局验证工具",
                    }.items():
                        page.locator("#" + field).fill(value)
                    form_frame("保存 Tool")
                    capture("tool-workspace-filled")
                    page.get_by_role("button", name="保存 Tool", exact=True).click()
                    expect(page.get_by_role("dialog")).not_to_be_visible()
                    body = last("/api/v1/catalog/tools")["body"]
                    assert (
                        body["contract"]["sourcePath"] == "layout_tool.py"
                        and body["contract"]["callableName"] == "sample_tool"
                    )
                    open_resource("tool", "添加 Python Tool")
                    page.locator("input[type=file]").set_input_files(tool_file)
                    page.get_by_role("button", name="只读检查 Callable", exact=True).click()
                    expect(page.locator(".python-tool-inspection-summary")).to_be_visible()
                    page.locator("#ptName").fill(f"uploaded_tool_{width}")
                    page.locator("#ptDisplayName").fill(f"上传工具 {width}")
                    form_frame("保存 Tool")
                    capture("tool-upload-inspected")
                    page.get_by_role("button", name="保存 Tool", exact=True).click()
                    expect(page.get_by_role("dialog")).not_to_be_visible()
                    assert any(
                        item["path"].endswith(":commit")
                        and item["body"]["name"] == f"uploaded_tool_{width}"
                        for item in requests
                        if item["body"]
                    )
                    open_resource("tool", "添加 Python Tool")
                    page.locator("#ptDisplayName").fill("放弃工具")
                    count = len(requests)
                    page.get_by_role("button", name="取消", exact=True).click()
                    assert len(requests) == count
                    capture("tool-saved-and-cancelled", actualLocalApi=True)

                    open_resource("mcp", "连接 MCP")
                    form_frame("保存并探测")
                    count = len(requests)
                    page.get_by_role("button", name="保存并探测", exact=True).click()
                    expect(page.locator("#mcpDisplayName")).to_have_attribute(
                        "aria-invalid", "true"
                    )
                    assert len(requests) == count
                    for field, value in {
                        "mcpDisplayName": f"布局 MCP {width}",
                        "mcpName": f"layout-mcp-{width}",
                        "mcpEndpoint": "https://mcp.example.com/mcp",
                        "mcpApiKey": f"LAYOUT_MCP_{width}",
                        "mcpApiKeyValue": "fixture-mcp-value",
                        "mcpDescription": "布局审计连接",
                    }.items():
                        page.locator("#" + field).fill(value)
                    form_frame("保存并探测")
                    capture("mcp-filled")
                    page.get_by_role("button", name="保存并探测", exact=True).click()
                    expect(page.get_by_role("dialog")).not_to_be_visible()
                    body = last("/api/v1/catalog/mcp-servers")["body"]
                    assert body["server"]["transport"] == "http"
                    assert body["server"]["endpointUrl"] == "https://mcp.example.com/mcp"
                    assert body["server"]["envRefs"] == {
                        "Authorization": f"env://LAYOUT_MCP_{width}"
                    }
                    open_resource("mcp", "连接 MCP")
                    page.get_by_role("combobox", name="Transport", exact=True).click()
                    page.get_by_role("option", name="STDIO（本地命令）", exact=True).click()
                    page.locator("#mcpCommand").fill("fixture-command")
                    page.locator("#mcpArgs").fill("--preview")
                    count = len(requests)
                    page.get_by_role("button", name="取消", exact=True).click()
                    assert len(requests) == count
                    capture("mcp-saved-and-cancelled", actualLocalApi=True, externalProbe="mocked")

                    open_resource("mcp", "连接 MCP")
                    count = len(requests)
                    paste_mcp("null")
                    expect(page.get_by_text("配置解析失败", exact=True)).to_be_visible()
                    expect(page.locator("#mcpName")).to_have_value("")
                    assert not page_errors, page_errors
                    assert len(requests) == count
                    paste_mcp("{invalid JSON")
                    expect(page.get_by_text("请粘贴有效的 JSON", exact=True)).to_be_visible()
                    expect(page.locator("#mcpName")).to_have_value("")
                    assert len(requests) == count
                    paste_mcp('{"mcpServers":{"broken":null}}')
                    expect(page.get_by_text("未找到 MCP server 定义", exact=True)).to_be_visible()
                    expect(page.locator("#mcpName")).to_have_value("")
                    assert len(requests) == count
                    capture("mcp-invalid-paste", rootSyntaxAndServerShapeValidated=True)

                    sse_name = f"layout-sse-{width}"
                    sse_label = f"布局 SSE {width}"
                    sse_credential = f"LAYOUT_SSE_{width}"
                    sse_config = json.dumps(
                        {
                            "mcpServers": {
                                sse_name: {
                                    "name": sse_label,
                                    "type": "sse",
                                    "url": "https://mcp.example.com/events",
                                    "description": "粘贴导入的 SSE 连接",
                                    "headers": {
                                        "Authorization": "Bearer ${" + sse_credential + "}"
                                    },
                                }
                            }
                        },
                        ensure_ascii=False,
                    )
                    paste_mcp(sse_config)
                    expect(page.locator("#mcpName")).to_have_value(sse_name)
                    expect(page.locator("#mcpDisplayName")).to_have_value(sse_label)
                    expect(page.get_by_role("combobox", name="Transport")).to_contain_text("SSE")
                    expect(page.locator("#mcpEndpoint")).to_have_value(
                        "https://mcp.example.com/events"
                    )
                    expect(page.locator("#mcpApiKey")).to_have_value(sse_credential)
                    expect(page.locator("#mcpDescription")).to_have_value("粘贴导入的 SSE 连接")
                    form_frame("保存并探测")
                    capture("mcp-valid-paste-cancel")
                    page.get_by_role("button", name="取消", exact=True).click()
                    assert len(requests) == count
                    open_resource("mcp", "连接 MCP")
                    expect(page.locator("#mcpName")).to_have_value("")
                    expect(page.locator("#mcpPaste")).to_have_value("")
                    paste_mcp(sse_config)
                    expect(page.locator("#mcpName")).to_have_value(sse_name)
                    page.locator("#mcpApiKeyValue").fill("fixture-sse-value")
                    form_frame("保存并探测")
                    page.get_by_role("button", name="保存并探测", exact=True).click()
                    expect(page.get_by_role("dialog")).not_to_be_visible()
                    body = last("/api/v1/catalog/mcp-servers")["body"]
                    assert body["displayName"] == sse_label
                    assert body["description"] == "粘贴导入的 SSE 连接"
                    assert body["server"] == {
                        "name": sse_name,
                        "version": "1.0.0",
                        "transport": "sse",
                        "args": [],
                        "envRefs": {"Authorization": f"env://{sse_credential}"},
                        "endpointUrl": "https://mcp.example.com/events",
                    }
                    reopen_mcp(sse_label, "sse", "https://mcp.example.com/events")

                    open_resource("mcp", "连接 MCP")
                    stdio_name = f"layout-stdio-{width}"
                    stdio_label = f"布局 STDIO {width}"
                    stdio_credential = f"LAYOUT_STDIO_{width}"
                    paste_mcp(
                        json.dumps(
                            {
                                "mcpServers": {
                                    stdio_name: {
                                        "name": stdio_label,
                                        "command": "fixture-command-never-executed",
                                        "args": ["--preview", "layout"],
                                        "env_key": stdio_credential,
                                    }
                                }
                            },
                            ensure_ascii=False,
                        )
                    )
                    expect(page.locator("#mcpName")).to_have_value(stdio_name)
                    expect(page.get_by_role("combobox", name="Transport")).to_contain_text("STDIO")
                    expect(page.locator("#mcpCommand")).to_have_value(
                        "fixture-command-never-executed"
                    )
                    expect(page.locator("#mcpArgs")).to_have_value("--preview layout")
                    expect(page.locator("#mcpApiKey")).to_have_value(stdio_credential)
                    page.locator("#mcpApiKeyValue").fill("fixture-stdio-value")
                    form_frame("保存并探测")
                    capture("mcp-stdio-filled")
                    page.get_by_role("button", name="保存并探测", exact=True).click()
                    expect(page.get_by_role("dialog")).not_to_be_visible()
                    body = last("/api/v1/catalog/mcp-servers")["body"]
                    assert body["displayName"] == stdio_label
                    assert body["server"] == {
                        "name": stdio_name,
                        "version": "1.0.0",
                        "transport": "stdio",
                        "args": ["--preview", "layout"],
                        "envRefs": {stdio_credential: f"env://{stdio_credential}"},
                        "command": "fixture-command-never-executed",
                    }
                    reopen_mcp(stdio_label, "stdio", "fixture-command-never-executed")

                    navigate("agents/conversation-items-agent/edit")
                    expect(page.locator("#editAgentPrompt")).to_be_visible()
                    expect(page.locator("#editAgentName")).to_have_attribute("readonly", "")
                    page.locator(".quick-create-actions input[type=checkbox]").uncheck()
                    count = len(requests)
                    page.locator("#editAgentPrompt").fill("x")
                    page.get_by_role("button", name="保存修改", exact=True).click()
                    expect(page.locator("#editAgentPrompt")).to_have_attribute(
                        "aria-invalid", "true"
                    )
                    assert len(requests) == count
                    prompt = f"布局审计：保留三个分类修改。视口 {width}。"
                    page.locator("#editAgentPrompt").fill(prompt)
                    page.get_by_role("button", name="能力绑定", exact=True).click()
                    page.get_by_role("button", name="选择绑定模型", exact=True).click()
                    page.get_by_role("option").filter(has_text=model_label).click()
                    page.keyboard.press("Escape")
                    page.get_by_role("combobox", name="默认模型", exact=True).click()
                    page.get_by_role("option").filter(has_text=model_label).click()
                    page.get_by_role("button", name="运行策略", exact=True).click()
                    page.locator("#editExecutionMaxSteps").fill("24")
                    page.locator("#editExecutionTimeout").fill("180")
                    page.get_by_role("combobox", name="执行策略", exact=True).click()
                    page.get_by_role("option", name="计划 · 执行 · 观察", exact=True).click()
                    page.get_by_role("button", name="基础与 Prompt", exact=True).click()
                    expect(page.locator("#editAgentPrompt")).to_have_value(prompt)
                    page.get_by_role("button", name="运行策略", exact=True).click()
                    expect(page.locator("#editExecutionMaxSteps")).to_have_value("24")
                    page.get_by_role(
                        "button", name="保存修改", exact=True
                    ).scroll_into_view_if_needed()
                    visible_control(page, page.get_by_role("button", name="保存修改", exact=True))
                    capture("agent-three-sections-filled")
                    page.get_by_role("button", name="保存修改", exact=True).click()
                    expect(
                        page.get_by_role("tablist", name="Agent 详情", exact=True)
                    ).to_be_visible()
                    body = last("/api/v1/agents/conversation-items-agent", "PUT")["body"]
                    assert body["instructions"]["system"] == prompt
                    assert (
                        body["execution"]["maxSteps"] == 24
                        and body["execution"]["timeoutSeconds"] == 180
                    )
                    assert body["execution"]["strategy"] == "plan-act-observe"
                    assert any(slug in resource for resource in body["bindings"]["modelProfileIds"])
                    navigate("agents/conversation-items-agent/edit")
                    expect(page.locator("#editAgentPrompt")).to_have_value(prompt)
                    page.locator("#editAgentPrompt").fill("离开编辑页时不保存的内容")
                    page.get_by_role("button", name="运行策略", exact=True).click()
                    page.locator("#editExecutionMaxSteps").fill("42")
                    page.get_by_role("button", name="能力绑定", exact=True).click()
                    page.get_by_role("button", name=f"移除 {model_label}", exact=True).click()
                    count = len(requests)
                    page.get_by_role("button", name="取消", exact=True).scroll_into_view_if_needed()
                    visible_control(page, page.get_by_role("button", name="取消", exact=True))
                    page.get_by_role("button", name="取消", exact=True).click()
                    expect(
                        page.get_by_role("tablist", name="Agent 详情", exact=True)
                    ).to_be_visible()
                    navigate("agents/conversation-items-agent/edit")
                    expect(page.locator("#editAgentPrompt")).to_have_value(prompt)
                    page.get_by_role("button", name="运行策略", exact=True).click()
                    expect(page.locator("#editExecutionMaxSteps")).to_have_value("24")
                    page.get_by_role("button", name="能力绑定", exact=True).click()
                    expect(
                        page.get_by_role("combobox", name="默认模型", exact=True)
                    ).to_contain_text(model_label)
                    expect(
                        page.get_by_role("button", name=f"移除 {model_label}", exact=True)
                    ).to_be_visible()
                    assert len(requests) == count
                    capture(
                        "agent-cancel-discards-three-sections",
                        actualLocalApi=True,
                        explicitCancelButton=True,
                    )
                    assert not external, external
                    assert not page_errors, page_errors
                    results.append(
                        {
                            "width": width,
                            "scenario": "request-scope",
                            "status": "passed",
                            "actualMutations": len(requests) - len(mocked),
                            "mockedProbePaths": mocked,
                            "unexpectedBrowserExternalRequests": len(external),
                        }
                    )
                except Exception:
                    if output:
                        page.screenshot(path=str(output / f"{width}-failure.png"))
                        (output / f"{width}-failure-aria.txt").write_text(
                            page.locator("body").aria_snapshot()
                        )
                        (output / f"{width}-page-errors.json").write_text(
                            json.dumps(page_errors, ensure_ascii=False, indent=2)
                        )
                        (output / "partial-results.json").write_text(
                            json.dumps(results, ensure_ascii=False, indent=2)
                        )
                    raise
                finally:
                    context.close()
            browser.close()
    if output:
        (output / "results.json").write_text(json.dumps(results, ensure_ascii=False, indent=2))
    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    arguments = parser.parse_args()
    run(arguments.output)

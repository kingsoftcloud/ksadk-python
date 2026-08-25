import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const { apiFetch } = vi.hoisted(() => ({ apiFetch: vi.fn() }));
function memoryStorage(): Storage {
  const values = new Map<string, string>();
  return {
    get length() { return values.size; },
    clear: () => values.clear(),
    getItem: key => values.get(key) ?? null,
    key: index => [...values.keys()][index] ?? null,
    removeItem: key => { values.delete(key); },
    setItem: (key, value) => { values.set(key, String(value)); },
  };
}
const defaultBuilds = [
  { id: "build-current", status: "SUCCEEDED" },
  { id: "build-previous", status: "SUCCEEDED" },
];
let agentBuilds: Array<{ id: string; status: string; createdAt?: string }> = defaultBuilds;
let currentCloudVersionId = "cloud-agent-1";
let deploymentRefreshFails = false;
let newDeploymentReady = false;
let listIncludesNewAgent = true;
let createNetworkFailures = 0;
let createNonTerminalPolls = 0;
let createTerminalFailures = 0;
let createStatusReadFailures = 0;
let createOperationPolls = 0;
let createIdempotencyKeys: string[] = [];
let createPollSignals: AbortSignal[] = [];
let staleOperationStatus = 404;
let bootstrapWorkspaceScope = "workspace-test";
let bootstrapCredentialScope = "credential-test";
let rollbackTerminalFailures = 0;
let rollbackIdempotencyKeys: string[] = [];
let accountAgentItems: Array<Record<string, unknown>> = [{
  agentId: "ar-cloud-ui",
  name: "Managed YAML Agent",
  status: "RUNNING",
  endpoint: "http://ar-cloud-ui.example.test",
  framework: "codex",
}];

apiFetch.mockImplementation(async (path: string, init?: RequestInit) => {
  if (path === "/api/v1/system/bootstrap") {
    return new Response(JSON.stringify({
      operationScope: {
        workspace: bootstrapWorkspaceScope,
        cloudCredential: bootstrapCredentialScope,
      },
    }));
  }
  if (path === "/api/v1/deployments") {
    const items = [{
      id: "dep-instance-1",
      buildId: "build-current",
      bundleDigest: "sha256:bundle-current",
      versionId: currentCloudVersionId,
      status: "READY",
      target: { region: "cn-beijing-6", environment: "preproduction" },
      agentId: "ar-cloud-ui",
      instanceId: "instance-1",
      endpoint: "http://ar-cloud-ui.example.test",
      artifactId: "managed-runtime",
    }];
    if (newDeploymentReady) items.push({
      id: "dep-instance-new",
      buildId: "build-new",
      bundleDigest: "sha256:bundle-new",
      versionId: "cloud-agent-new-v1",
      status: "READY",
      target: { region: "cn-beijing-6", environment: "cloud" },
      agentId: "ar-cloud-new",
      instanceId: "instance-new",
      endpoint: "http://ar-cloud-new.example.test",
      artifactId: "managed-runtime",
    });
    return new Response(JSON.stringify({ items }));
  }
  if (path === "/api/v1/cloud-agents?size=100") {
    return new Response(JSON.stringify({ items: accountAgentItems, total: accountAgentItems.length }));
  }
  if (path === "/api/v1/cloud-agents/ar-existing-code" && init?.method === "DELETE") {
    return new Response(JSON.stringify({ agentId: "ar-existing-code", deletedReceiptIds: [] }));
  }
  if (path === "/api/v1/cloud-agents/ar-existing-code" && !init?.method) {
    return new Response(JSON.stringify({
      agentId: "ar-existing-code", name: "Existing Code Agent", status: "RUNNING",
      endpoint: "http://existing-code.example.test", framework: "langgraph",
      versionId: "version-existing",
    }));
  }
  if (path === "/api/v1/cloud-agents/ar-cloud-ui/versions?page=1&size=100") {
    const firstIsCurrent = currentCloudVersionId === "cloud-agent-1";
    return new Response(JSON.stringify({ currentVersionId: currentCloudVersionId, items: [
      {
        versionId: "cloud-agent-1", versionName: "v3", tag: "release-v3",
        status: firstIsCurrent ? "current" : "historical", trafficPercentage: firstIsCurrent ? 100 : 0,
        createdAt: "2026-08-24T10:00:00+08:00", createdBy: "studio",
        canRollback: !firstIsCurrent,
        rollbackDisabledReason: firstIsCurrent ? "当前版本不可回滚至自身" : "",
      },
      {
        versionId: "cloud-agent-0", versionName: "v2", tag: "release-v2",
        status: firstIsCurrent ? "historical" : "current", trafficPercentage: firstIsCurrent ? 0 : 100,
        createdAt: "2026-08-23T10:00:00+08:00", createdBy: "studio",
        canRollback: firstIsCurrent,
        rollbackDisabledReason: firstIsCurrent ? "" : "当前版本不可回滚至自身",
      },
    ], total: 2 }));
  }
  if (path === "/api/v1/cloud-agents/ar-cloud-ui" && !init?.method) {
    return new Response(JSON.stringify({
      agentId: "ar-cloud-ui", name: "Managed YAML Agent", status: "RUNNING",
      endpoint: "http://ar-cloud-ui.example.test", framework: "codex",
      versionId: currentCloudVersionId, updatedAt: "2026-08-24T11:00:00+08:00",
    }));
  }
  if (path === "/api/v1/cloud-agents/ar-existing-code/versions?page=1&size=100") {
    return new Response(JSON.stringify({ items: [{
      versionId: "version-existing", versionName: "v1", tag: "release-v1",
      status: "current", trafficPercentage: 100, canRollback: false,
      rollbackDisabledReason: "当前版本不可回滚至自身",
    }], total: 1 }));
  }
  if (path === "/api/v1/cloud-agents/ar-existing-code:dashboard") {
    return new Response(JSON.stringify({ accessUrl: "https://dashboard.example.test/existing" }));
  }
  if ([
    "/api/v1/cloud-agents/ar-hermes:dashboard",
    "/api/v1/cloud-agents/ar-openclaw:dashboard",
  ].includes(path)) {
    const agentId = path.split("/").at(-1)!.split(":", 1)[0];
    return new Response(JSON.stringify({ accessUrl: `https://dashboard.example.test/${agentId}` }));
  }
  if (path === "/api/v1/deployments/dep-instance-1" && !init?.method) {
    if (deploymentRefreshFails) return new Response("refresh failed", { status: 500 });
    return new Response(JSON.stringify({
      id: "dep-instance-1", buildId: "build-current", bundleDigest: "sha256:bundle-current",
      versionId: currentCloudVersionId, status: "DEPLOYING",
      target: { region: "cn-beijing-6", environment: "preproduction" },
      agentId: "ar-cloud-ui", instanceId: "instance-1",
      endpoint: "http://ar-cloud-ui.example.test", artifactId: "managed-runtime",
    }));
  }
  if (path === "/api/v1/deployments/dep-instance-2" && !init?.method) {
    return new Response(JSON.stringify({
      id: "dep-instance-2", buildId: "build-previous", bundleDigest: "sha256:bundle-previous",
      versionId: "cloud-agent-2", status: "READY",
      target: { region: "cn-beijing-6", environment: "preproduction" },
      agentId: "ar-cloud-ui", instanceId: "instance-2",
      endpoint: "http://ar-cloud-ui.example.test", artifactId: "managed-runtime",
    }));
  }
  if (["/api/v1/builds/build-current", "/api/v1/builds/build-previous"].includes(path)) {
    return new Response(JSON.stringify({ agentId: "demo-agent" }));
  }
  if (path === "/api/v1/system/settings") {
    return new Response(JSON.stringify({ cloudRegion: "cn-beijing-6" }));
  }
  if (path === "/api/v1/agents?limit=100") {
    return new Response(JSON.stringify({ items: [
      { metadata: { id: "demo-agent", name: "Demo Agent" } },
      ...(listIncludesNewAgent ? [{ metadata: { id: "new-agent", name: "New Agent" } }] : []),
    ] }));
  }
  if (path === "/api/v1/builds/build-new") {
    return new Response(JSON.stringify({
      id: "build-new", agentId: "new-agent", status: "SUCCEEDED",
      bundleDigest: "sha256:bundle-new", createdAt: "2026-08-24T12:00:00Z",
      runtimeName: "codex", runtimeVersion: "0.144.4",
    }));
  }
  if (path === "/api/v1/agents/demo-agent") return new Response(JSON.stringify({
    draft: { metadata: { id: "demo-agent", name: "Demo Agent" }, spec: { runtime: { type: "langgraph" } } },
    builds: agentBuilds,
  }));
  if (path === "/api/v1/agents/new-agent") return new Response(JSON.stringify({
    draft: { metadata: { id: "new-agent", name: "New Agent", labels: { "agentkit.ksyun.com/artifact-type": "ManagedRuntime" } }, spec: { runtime: { type: "codex" } } },
    builds: [{
      id: "build-new", status: "SUCCEEDED", bundleDigest: "sha256:bundle-new",
      createdAt: "2026-08-24T12:00:00Z", runtimeName: "codex", runtimeVersion: "0.144.4",
    }],
  }));
  if (path === "/api/v1/builds/build-new/deployments") {
    expect(init?.method).toBe("POST");
    createIdempotencyKeys.push(new Headers(init?.headers).get("Idempotency-Key") || "");
    if (createNetworkFailures > 0) {
      createNetworkFailures -= 1;
      throw new TypeError("network disconnected after request write");
    }
    expect(JSON.parse(String(init?.body))).toEqual({
      target: { region: "cn-beijing-6", environment: "cloud" },
      releasePolicy: { strategy: "rolling", approval: "none" },
    });
    return new Response(JSON.stringify({ id: "operation-create" }));
  }
  if (path === "/api/v1/builds/build-latest/deployments") {
    expect(init?.method).toBe("POST");
    return new Response(JSON.stringify({ id: "operation-update" }));
  }
  if (path === "/api/v1/cloud-agents/ar-cloud-ui:rollback-version") {
    expect(init?.method).toBe("POST");
    rollbackIdempotencyKeys.push(new Headers(init?.headers).get("Idempotency-Key") || "");
    expect(JSON.parse(String(init?.body))).toEqual({ versionId: "cloud-agent-0" });
    return new Response(JSON.stringify({ id: "operation-rollback" }));
  }
  if (path === "/api/v1/deployments/dep-instance-1:dashboard") {
    expect(init?.method).toBe("POST");
    return new Response(JSON.stringify({ accessUrl: "https://dashboard.example.test/private-link" }));
  }
  if (path === "/api/v1/deployments/dep-instance-1" && init?.method === "DELETE") {
    return new Response(JSON.stringify({ agentId: "ar-cloud-ui", deletedReceiptIds: ["dep-instance-1"] }));
  }
  if (path === "/api/v1/operations/operation-rollback") {
    if (rollbackTerminalFailures > 0) {
      rollbackTerminalFailures -= 1;
      return new Response(JSON.stringify({ status: "FAILED", error: { message: "rollout failed" } }));
    }
    currentCloudVersionId = "cloud-agent-0";
    return new Response(JSON.stringify({ status: "SUCCEEDED", resourceId: "ar-cloud-ui" }));
  }
  if (path === "/api/v1/operations/operation-update") {
    return new Response(JSON.stringify({ status: "SUCCEEDED", resourceId: "dep-instance-2" }));
  }
  if (path === "/api/v1/operations/operation-create") {
    if (init?.signal) createPollSignals.push(init.signal);
    createOperationPolls += 1;
    if (createStatusReadFailures > 0) {
      createStatusReadFailures -= 1;
      throw new TypeError("operation status network error");
    }
    if (createOperationPolls <= createNonTerminalPolls) {
      return new Response(JSON.stringify({ status: "RUNNING", resourceId: "" }));
    }
    if (createTerminalFailures > 0) {
      createTerminalFailures -= 1;
      return new Response(JSON.stringify({ status: "FAILED", error: { message: "admission failed" } }));
    }
    newDeploymentReady = true;
    return new Response(JSON.stringify({ status: "SUCCEEDED", resourceId: "dep-instance-new" }));
  }
  if (path === "/api/v1/operations/operation-gone") {
    return new Response("operation no longer exists", { status: staleOperationStatus });
  }
  throw new Error(path);
});

vi.mock("../api", () => ({ apiFetch }));

import { DeploymentsPage } from "./DeploymentsPage";

describe("DeploymentsPage", () => {
  beforeEach(() => {
    Object.defineProperty(window, "localStorage", {
      configurable: true,
      value: memoryStorage(),
    });
    window.history.replaceState(null, "", "#/deployments");
    agentBuilds = defaultBuilds;
    currentCloudVersionId = "cloud-agent-1";
    deploymentRefreshFails = false;
    newDeploymentReady = false;
    listIncludesNewAgent = true;
    createNetworkFailures = 0;
    createNonTerminalPolls = 0;
    createTerminalFailures = 0;
    createStatusReadFailures = 0;
    createOperationPolls = 0;
    createIdempotencyKeys = [];
    createPollSignals = [];
    staleOperationStatus = 404;
    bootstrapWorkspaceScope = "workspace-test";
    bootstrapCredentialScope = "credential-test";
    rollbackTerminalFailures = 0;
    rollbackIdempotencyKeys = [];
    accountAgentItems = [{
      agentId: "ar-cloud-ui",
      name: "Managed YAML Agent",
      status: "RUNNING",
      endpoint: "http://ar-cloud-ui.example.test",
      framework: "codex",
    }];
    window.localStorage.clear();
    vi.clearAllMocks();
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  const renderPage = (onOpenChat = vi.fn(), onSelectBuild = vi.fn()) => render(
    <DeploymentsPage onCreate={vi.fn()} onOpenChat={onOpenChat} onSelectBuild={onSelectBuild} />,
  );

  it("keeps the Server cloud projection authoritative after refreshing a local receipt", async () => {
    const user = userEvent.setup();
    renderPage();

    expect(await screen.findByText("ar-cloud-ui")).toBeInTheDocument();
    expect(screen.queryByText("preproduction")).not.toBeInTheDocument();
    expect(screen.queryByText("cn-beijing-6")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Managed YAML Agent 的更多操作" }));
    await user.click(await screen.findByRole("menuitem", { name: "刷新状态" }));
    await waitFor(() => expect(apiFetch).toHaveBeenCalledWith("/api/v1/deployments/dep-instance-1"));
    await waitFor(() => expect(apiFetch).toHaveBeenCalledWith("/api/v1/cloud-agents/ar-cloud-ui"));
    expect(await screen.findAllByText("运行中")).not.toHaveLength(0);
    expect(screen.queryByText("部署中")).not.toBeInTheDocument();

    expect(screen.queryByRole("region", { name: "选择回滚 Build" })).not.toBeInTheDocument();
  });

  it("opens cloud chat inside Studio and keeps Hosted UI in more actions", async () => {
    const user = userEvent.setup();
    const open = vi.fn();
    const onOpenChat = vi.fn();
    vi.stubGlobal("open", open);
    renderPage(onOpenChat);

    fireEvent.click(await screen.findByRole("button", { name: "打开云端 Agent 会话" }));
    expect(onOpenChat).toHaveBeenCalledWith("dep-instance-1");
    expect(screen.queryByRole("menuitem", { name: "在 Hosted UI 中打开" })).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Managed YAML Agent 的更多操作" }));
    await user.click(await screen.findByRole("menuitem", { name: "在 Hosted UI 中打开" }));

    await waitFor(() => expect(apiFetch).toHaveBeenCalledWith(
      "/api/v1/deployments/dep-instance-1:dashboard",
      { method: "POST" },
    ));
    expect(open).toHaveBeenCalledWith(
      "https://dashboard.example.test/private-link",
      "_blank",
      "noopener,noreferrer",
    );
    vi.unstubAllGlobals();
  });

  it.each(["hermes", "openclaw"])(
    "opens %s in its official Dashboard instead of Studio cloud chat",
    async framework => {
      const user = userEvent.setup();
      const onOpenChat = vi.fn();
      const open = vi.fn();
      vi.stubGlobal("open", open);
      accountAgentItems = [{
        agentId: `ar-${framework}`,
        name: `${framework} Agent`,
        status: "RUNNING",
        framework,
      }];
      renderPage(onOpenChat);

      await user.click(await screen.findByRole("button", { name: "打开官方 Dashboard" }));

      expect(onOpenChat).not.toHaveBeenCalled();
      await waitFor(() => expect(apiFetch).toHaveBeenCalledWith(
        `/api/v1/cloud-agents/ar-${framework}:dashboard`,
        { method: "POST" },
      ));
      expect(open).toHaveBeenCalledWith(
        `https://dashboard.example.test/ar-${framework}`,
        "_blank",
        "noopener,noreferrer",
      );
      vi.unstubAllGlobals();
    },
  );

  it("shows receipt-bound cloud details and Server-projected cloud version history", async () => {
    const user = userEvent.setup();
    const expectedVersionTime = new Date("2026-08-23T10:00:00+08:00")
      .toLocaleString("zh-CN", { hour12: false });
    renderPage();

    await user.click(await screen.findByRole("button", { name: "查看 Managed YAML Agent 详情" }));

    expect(window.location.hash).toBe("#/deployments/dep-instance-1");
    expect(await screen.findByRole("heading", { name: "Managed YAML Agent" })).toBeInTheDocument();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "详情" })).not.toBeInTheDocument();
    expect(screen.getByTitle("cloud-agent-1")).toHaveTextContent("v3");
    expect(screen.getAllByText("ar-cloud-ui").length).toBeGreaterThanOrEqual(2);
    expect(screen.getByText("http://ar-cloud-ui.example.test")).toBeInTheDocument();
    expect(screen.getByText("运行中")).toBeInTheDocument();
    expect(screen.queryByText("部署中")).not.toBeInTheDocument();
    expect(await screen.findByRole("region", { name: "云端版本历史" })).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: /当前版本.*v3/ })).toHaveTextContent("当前");
    expect(screen.getByText("流量")).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: /当前版本.*v3/ })).toHaveTextContent("100%");
    expect(screen.getByRole("radio", { name: /可回滚版本.*v2/ })).toHaveTextContent(expectedVersionTime);
    expect(screen.queryByText("cloud-agent-0")).not.toBeInTheDocument();
    expect(apiFetch).toHaveBeenCalledWith("/api/v1/cloud-agents/ar-cloud-ui/versions?page=1&size=100");
    expect(apiFetch).toHaveBeenCalledWith("/api/v1/cloud-agents/ar-cloud-ui");
  });

  it("renders each cloud version as one compact selectable row without native radio sizing", async () => {
    const user = userEvent.setup();
    renderPage();

    await user.click(await screen.findByRole("button", { name: "查看 Managed YAML Agent 详情" }));

    const current = await screen.findByRole("radio", { name: /当前版本.*v3/ });
    const historical = screen.getByRole("radio", { name: /可回滚版本.*v2/ });
    expect(current.tagName).toBe("BUTTON");
    expect(current).toBeDisabled();
    expect(historical.tagName).toBe("BUTTON");
    expect(historical).toHaveAttribute("aria-checked", "false");
    await user.click(historical);
    expect(historical).toHaveAttribute("aria-checked", "true");
    expect(screen.getByRole("button", { name: "回滚到所选版本" })).toBeEnabled();
  });

  it("keeps Server version history usable when a stale local receipt cannot refresh", async () => {
    const user = userEvent.setup();
    deploymentRefreshFails = true;
    renderPage();

    await user.click(await screen.findByRole("button", { name: "查看 Managed YAML Agent 详情" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("刷新云端 Agent 状态失败（500）");
    expect(screen.getByRole("radio", { name: /当前版本.*v3/ })).toBeDisabled();
    const historical = screen.getByRole("radio", { name: /可回滚版本.*v2/ });
    await user.click(historical);
    expect(screen.getByRole("button", { name: "回滚到所选版本" })).toBeEnabled();
  });

  it("obeys Server CanRollback and refreshes Agent plus ListVersions after rollback", async () => {
    const user = userEvent.setup();
    renderPage();

    await user.click(await screen.findByRole("button", { name: "查看 Managed YAML Agent 详情" }));

    const rollbackButton = await screen.findByRole("button", { name: "选择版本回滚" });
    expect(rollbackButton).toBeDisabled();
    expect(screen.getByRole("radio", { name: /当前版本.*v3/ })).toBeDisabled();

    await user.click(screen.getByRole("radio", { name: /可回滚版本.*v2/ }));
    expect(screen.getByRole("button", { name: "回滚到所选版本" })).toBeEnabled();
    await user.click(screen.getByRole("button", { name: "回滚到所选版本" }));

    expect(screen.getByRole("alertdialog")).toHaveTextContent("cloud-agent-1");
    expect(screen.getByRole("alertdialog")).toHaveTextContent("cloud-agent-0");
    await user.click(screen.getByRole("button", { name: "确认回滚" }));

    await waitFor(() => expect(apiFetch).toHaveBeenCalledWith(
      "/api/v1/cloud-agents/ar-cloud-ui:rollback-version",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ versionId: "cloud-agent-0" }),
      }),
    ));
    await waitFor(() => expect(screen.getByRole("radio", { name: /当前版本.*v2/ })).toBeDisabled());
    expect(window.location.hash).toBe("#/deployments/dep-instance-1");
  });

  it("restores an Agent detail page from its deployment route", async () => {
    window.history.replaceState(null, "", "#/deployments/dep-instance-1");
    renderPage();

    expect(await screen.findByRole("heading", { name: "Managed YAML Agent" })).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "云端 Agent 详情" })).toBeInTheDocument();
  });

  it("deletes the receipt-bound cloud Agent after explicit confirmation", async () => {
    const user = userEvent.setup();
    renderPage();

    await user.click(await screen.findByRole("button", { name: "Managed YAML Agent 的更多操作" }));
    await user.click(await screen.findByRole("menuitem", { name: "删除云端 Agent" }));
    await user.click(await screen.findByRole("button", { name: "删除云端 Agent" }));

    await waitFor(() => expect(apiFetch).toHaveBeenCalledWith(
      "/api/v1/deployments/dep-instance-1",
      { method: "DELETE" },
    ));
    await waitFor(() => expect(
      screen.queryByRole("heading", { name: "删除云端 Agent？" }),
    ).not.toBeInTheDocument());
  });

  it("updates a managed cloud Agent only through an existing successful Build", async () => {
    const user = userEvent.setup();
    agentBuilds = [
      { id: "build-latest", status: "SUCCEEDED", createdAt: "2026-08-24T10:00:00Z" },
      { id: "build-current", status: "SUCCEEDED", createdAt: "2026-08-23T10:00:00Z" },
    ];
    renderPage();

    await user.click(await screen.findByRole("button", { name: "查看 Managed YAML Agent 详情" }));
    await user.click(await screen.findByRole("button", { name: "部署最新 Build" }));

    await waitFor(() => expect(apiFetch).toHaveBeenCalledWith(
      "/api/v1/builds/build-latest/deployments",
      expect.objectContaining({ method: "POST" }),
    ));
  });

  it("deduplicates account discovery by agentId and keeps account-only Agents manageable", async () => {
    const user = userEvent.setup();
    const onOpenChat = vi.fn();
    const open = vi.fn();
    vi.stubGlobal("open", open);
    accountAgentItems = [
      { agentId: "ar-cloud-ui", name: "Duplicate Receipt Agent", status: "RUNNING" },
      {
        agentId: "ar-existing-code", name: "Existing Code Agent", status: "RUNNING",
        endpoint: "http://existing-code.example.test", framework: "langgraph",
        versionId: "version-existing",
      },
    ];
    renderPage(onOpenChat);

    expect(await screen.findByText("Existing Code Agent")).toBeInTheDocument();
    expect(screen.getAllByText("ar-cloud-ui")).toHaveLength(1);
    expect(screen.getByText("账号云端 Agent")).toBeInTheDocument();
    expect(screen.queryByText("sha256:bundle-current")).not.toBeInTheDocument();
    expect(screen.queryByText("build-current")).not.toBeInTheDocument();

    const chatButtons = screen.getAllByRole("button", { name: "打开云端 Agent 会话" });
    fireEvent.click(chatButtons[1]);
    expect(onOpenChat).toHaveBeenCalledWith("account:ar-existing-code");

    await user.click(screen.getByRole("button", { name: "查看 Existing Code Agent 详情" }));
    expect(await screen.findByRole("region", { name: "云端版本历史" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "部署最新 Build" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "选择版本回滚" })).toBeDisabled();
    await user.click(screen.getAllByRole("button", { name: "返回云端 Agent" }).at(-1)!);
    expect(window.location.hash).toBe("#/deployments");

    await user.click(screen.getByRole("button", { name: "Existing Code Agent 的更多操作" }));
    await user.click(await screen.findByRole("menuitem", { name: "在 Hosted UI 中打开" }));
    await waitFor(() => expect(open).toHaveBeenCalledWith(
      "https://dashboard.example.test/existing", "_blank", "noopener,noreferrer",
    ));

    await user.click(screen.getByRole("button", { name: "Existing Code Agent 的更多操作" }));
    await user.click(await screen.findByRole("menuitem", { name: "删除云端 Agent" }));
    await user.click(await screen.findByRole("button", { name: "删除云端 Agent" }));
    await waitFor(() => expect(apiFetch).toHaveBeenCalledWith(
      "/api/v1/cloud-agents/ar-existing-code", { method: "DELETE" },
    ));
    vi.unstubAllGlobals();
  });

  it("selects a successful Build and submits a cloud deployment from the deployment page", async () => {
    const user = userEvent.setup();
    window.history.replaceState(null, "", "#/deployments/new");
    renderPage();

    await user.click(await screen.findByRole("radio", { name: /New Agent.*build-new/ }));
    await user.click(screen.getByRole("button", { name: "部署到云端" }));

    await waitFor(() => expect(apiFetch).toHaveBeenCalledWith(
      "/api/v1/builds/build-new/deployments",
      expect.objectContaining({ method: "POST" }),
    ));
    await waitFor(() => expect(window.location.hash).toBe("#/deployments/dep-instance-new"));
  });

  it("keeps following a durable operation beyond the former 30 second polling window", async () => {
    window.history.replaceState(null, "", "#/deployments/new");
    createNonTerminalPolls = 151;
    renderPage();
    const build = await screen.findByRole("radio", { name: /New Agent.*build-new/ });
    fireEvent.click(build);
    vi.useFakeTimers();
    fireEvent.click(screen.getByRole("button", { name: "部署到云端" }));
    await vi.advanceTimersByTimeAsync(152_000);

    expect(window.location.hash).toBe("#/deployments/dep-instance-new");
    expect(createOperationPolls).toBeGreaterThan(150);
    expect(screen.queryByText("部署操作等待超时")).not.toBeInTheDocument();
  });

  it("reuses one idempotency key when an ambiguous network submission is retried", async () => {
    const user = userEvent.setup();
    window.history.replaceState(null, "", "#/deployments/new");
    createNetworkFailures = 1;
    renderPage();

    await user.click(await screen.findByRole("radio", { name: /New Agent.*build-new/ }));
    await user.click(screen.getByRole("button", { name: "部署到云端" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("network disconnected");
    await user.click(screen.getByRole("button", { name: "部署到云端" }));

    await waitFor(() => expect(createIdempotencyKeys).toHaveLength(2));
    expect(createIdempotencyKeys[1]).toBe(createIdempotencyKeys[0]);
    await waitFor(() => expect(window.location.hash).toBe("#/deployments/dep-instance-new"));
  });

  it("uses a fresh idempotency key for an explicit new attempt after terminal failure", async () => {
    const user = userEvent.setup();
    window.history.replaceState(null, "", "#/deployments/new");
    createTerminalFailures = 1;
    renderPage();

    await user.click(await screen.findByRole("radio", { name: /New Agent.*build-new/ }));
    await user.click(screen.getByRole("button", { name: "部署到云端" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("admission failed");
    await user.click(screen.getByRole("button", { name: "部署到云端" }));

    await waitFor(() => expect(createIdempotencyKeys).toHaveLength(2));
    expect(createIdempotencyKeys[1]).not.toBe(createIdempotencyKeys[0]);
    await waitFor(() => expect(window.location.hash).toBe("#/deployments/dep-instance-new"));
  });

  it("resumes a persisted Server operation instead of submitting a duplicate after status recovery", async () => {
    const user = userEvent.setup();
    window.history.replaceState(null, "", "#/deployments/new");
    createStatusReadFailures = 1;
    const firstRender = renderPage();

    await user.click(await screen.findByRole("radio", { name: /New Agent.*build-new/ }));
    await user.click(screen.getByRole("button", { name: "部署到云端" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("operation status network error");
    expect(createIdempotencyKeys).toHaveLength(1);

    firstRender.unmount();
    renderPage();
    await user.click(await screen.findByRole("radio", { name: /New Agent.*build-new/ }));
    await user.click(screen.getByRole("button", { name: "部署到云端" }));

    await waitFor(() => expect(window.location.hash).toBe("#/deployments/dep-instance-new"));
    expect(createIdempotencyKeys).toHaveLength(1);
  });

  it.each([403, 404, 410])("clears a %i persisted operation before allowing an explicit retry", async status => {
    const user = userEvent.setup();
    staleOperationStatus = status;
    window.history.replaceState(null, "", "#/deployments/new");
    const storageKey = "agentkit-studio:deployment-operation-attempts:v2:workspace-test:credential-test";
    window.localStorage.setItem(storageKey, JSON.stringify({
      "deploy:build-new:cn-beijing-6": {
        actionKey: "deploy:build-new:cn-beijing-6",
        idempotencyKey: "stale-key",
        operationId: "operation-gone",
      },
    }));
    renderPage();

    await user.click(await screen.findByRole("radio", { name: /New Agent.*build-new/ }));
    await user.click(screen.getByRole("button", { name: "部署到云端" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(`部署状态读取失败（${status}）`);
    expect(window.localStorage.getItem(storageKey)).toBe("{}");

    await user.click(screen.getByRole("button", { name: "部署到云端" }));
    await waitFor(() => expect(createIdempotencyKeys).toHaveLength(1));
    expect(createIdempotencyKeys[0]).not.toBe("stale-key");
  });

  it.each([
    ["another workspace", "workspace-other", "credential-test"],
    ["another cloud account", "workspace-test", "credential-other"],
  ])("isolates resumable attempts from %s", async (_label, workspaceScope, credentialScope) => {
    const user = userEvent.setup();
    window.history.replaceState(null, "", "#/deployments/new");
    createNetworkFailures = 1;
    window.localStorage.setItem(
      "agentkit-studio:deployment-operation-attempts:v2:workspace-test:credential-test",
      JSON.stringify({
        "deploy:build-new:cn-beijing-6": {
          actionKey: "deploy:build-new:cn-beijing-6",
          idempotencyKey: "foreign-key",
          operationId: "operation-gone",
        },
      }),
    );
    bootstrapWorkspaceScope = workspaceScope;
    bootstrapCredentialScope = credentialScope;
    renderPage();

    await user.click(await screen.findByRole("radio", { name: /New Agent.*build-new/ }));
    await user.click(screen.getByRole("button", { name: "部署到云端" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("network disconnected");

    const scopedKey = `agentkit-studio:deployment-operation-attempts:v2:${workspaceScope}:${credentialScope}`;
    expect(JSON.parse(window.localStorage.getItem(scopedKey) || "{}")).toMatchObject({
      "deploy:build-new:cn-beijing-6": { operationId: "" },
    });
    expect(window.localStorage.getItem("agentkit-studio:deployment-operation-attempts:v1")).toBeNull();
  });

  it("aborts durable polling when the deployment page unmounts", async () => {
    window.history.replaceState(null, "", "#/deployments/new");
    createNonTerminalPolls = Number.MAX_SAFE_INTEGER;
    const page = renderPage();
    fireEvent.click(await screen.findByRole("radio", { name: /New Agent.*build-new/ }));
    vi.useFakeTimers();
    fireEvent.click(screen.getByRole("button", { name: "部署到云端" }));
    await vi.advanceTimersByTimeAsync(1_000);
    expect(createOperationPolls).toBeGreaterThan(0);
    const pollsAtUnmount = createOperationPolls;

    page.unmount();
    await vi.advanceTimersByTimeAsync(5_000);

    expect(createOperationPolls).toBe(pollsAtUnmount);
    expect(createPollSignals.at(-1)?.aborted).toBe(true);
    expect(window.location.hash).toBe("#/deployments/new");
  });

  it("serializes first submission across tabs so only one POST creates the operation", async () => {
    const queues = new Map<string, Promise<unknown>>();
    const request = vi.fn(async <T,>(
      name: string,
      _options: LockOptions,
      callback: () => Promise<T>,
    ): Promise<T> => {
      const previous = queues.get(name) || Promise.resolve();
      let release!: () => void;
      const current = new Promise<void>(resolve => { release = resolve; });
      queues.set(name, previous.then(() => current));
      await previous;
      try {
        return await callback();
      } finally {
        release();
      }
    });
    Object.defineProperty(navigator, "locks", { configurable: true, value: { request } });
    window.history.replaceState(null, "", "#/deployments/new");
    const first = renderPage();
    const second = renderPage();

    const builds = await screen.findAllByRole("radio", { name: /New Agent.*build-new/ });
    builds.forEach(build => fireEvent.click(build));
    screen.getAllByRole("button", { name: "部署到云端" }).forEach(button => fireEvent.click(button));

    await waitFor(() => expect(window.location.hash).toBe("#/deployments/dep-instance-new"));
    expect(request).toHaveBeenCalled();
    expect(createIdempotencyKeys).toHaveLength(1);
    first.unmount();
    second.unmount();
    Reflect.deleteProperty(navigator, "locks");
  });

  it("uses a fresh rollback key when the user explicitly retries a FAILED operation", async () => {
    const user = userEvent.setup();
    rollbackTerminalFailures = 1;
    renderPage();

    await user.click(await screen.findByRole("button", { name: "查看 Managed YAML Agent 详情" }));
    await user.click(screen.getByRole("radio", { name: /可回滚版本.*v2/ }));
    await user.click(screen.getByRole("button", { name: "回滚到所选版本" }));
    await user.click(screen.getByRole("button", { name: "确认回滚" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("rollout failed");

    await user.click(screen.getByRole("button", { name: "回滚到所选版本" }));
    await user.click(screen.getByRole("button", { name: "确认回滚" }));

    await waitFor(() => expect(rollbackIdempotencyKeys).toHaveLength(2));
    expect(rollbackIdempotencyKeys[1]).not.toBe(rollbackIdempotencyKeys[0]);
  });

  it("restores the precise successful Build selected by the Build page", async () => {
    listIncludesNewAgent = false;
    window.history.replaceState(null, "", "#/deployments/new?buildId=build-new&agentId=new-agent");
    renderPage();

    expect(await screen.findByRole("radio", { name: /New Agent.*build-new/ })).toHaveAttribute("aria-checked", "true");
    expect(screen.getByRole("button", { name: "部署到云端" })).toBeEnabled();
    expect(screen.queryByRole("heading", { name: "云端 Agent" })).not.toBeInTheDocument();
  });
});

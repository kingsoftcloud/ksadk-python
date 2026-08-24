import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

const { apiFetch } = vi.hoisted(() => ({ apiFetch: vi.fn() }));
const defaultBuilds = [
  { id: "build-current", status: "SUCCEEDED" },
  { id: "build-previous", status: "SUCCEEDED" },
];
let agentBuilds: Array<{ id: string; status: string; createdAt?: string }> = defaultBuilds;
let currentCloudVersionId = "cloud-agent-1";
let accountAgentItems: Array<Record<string, unknown>> = [{
  agentId: "ar-cloud-ui",
  name: "Managed YAML Agent",
  status: "RUNNING",
  endpoint: "http://ar-cloud-ui.example.test",
  framework: "codex",
}];

apiFetch.mockImplementation(async (path: string, init?: RequestInit) => {
  if (path === "/api/v1/deployments") {
    return new Response(JSON.stringify({ items: [{
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
    }] }));
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
    return new Response(JSON.stringify({ items: [
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
  if (path === "/api/v1/agents/demo-agent") return new Response(JSON.stringify({
    builds: agentBuilds,
  }));
  if (path === "/api/v1/builds/build-latest/deployments") {
    expect(init?.method).toBe("POST");
    return new Response(JSON.stringify({ id: "operation-update" }));
  }
  if (path === "/api/v1/cloud-agents/ar-cloud-ui:rollback-version") {
    expect(init?.method).toBe("POST");
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
    currentCloudVersionId = "cloud-agent-0";
    return new Response(JSON.stringify({ status: "SUCCEEDED", resourceId: "ar-cloud-ui" }));
  }
  if (path === "/api/v1/operations/operation-update") {
    return new Response(JSON.stringify({ status: "SUCCEEDED", resourceId: "dep-instance-2" }));
  }
  throw new Error(path);
});

vi.mock("../api", () => ({ apiFetch }));

import { DeploymentsPage } from "./DeploymentsPage";

describe("DeploymentsPage", () => {
  beforeEach(() => {
    window.history.replaceState(null, "", "#/deployments");
    agentBuilds = defaultBuilds;
    currentCloudVersionId = "cloud-agent-1";
    accountAgentItems = [{
      agentId: "ar-cloud-ui",
      name: "Managed YAML Agent",
      status: "RUNNING",
      endpoint: "http://ar-cloud-ui.example.test",
      framework: "codex",
    }];
    vi.clearAllMocks();
  });

  const renderPage = (onOpenChat = vi.fn(), onSelectBuild = vi.fn()) => render(
    <DeploymentsPage onCreate={vi.fn()} onOpenChat={onOpenChat} onSelectBuild={onSelectBuild} />,
  );

  it("refreshes a Server-projected status from the deployment list", async () => {
    const user = userEvent.setup();
    renderPage();

    expect(await screen.findByText("ar-cloud-ui")).toBeInTheDocument();
    expect(screen.queryByText("preproduction")).not.toBeInTheDocument();
    expect(screen.queryByText("cn-beijing-6")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Managed YAML Agent 的更多操作" }));
    await user.click(await screen.findByRole("menuitem", { name: "刷新状态" }));
    await waitFor(() => expect(apiFetch).toHaveBeenCalledWith("/api/v1/deployments/dep-instance-1"));
    expect(await screen.findByText("部署中")).toBeInTheDocument();

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
    renderPage();

    await user.click(await screen.findByRole("button", { name: "查看 Managed YAML Agent 详情" }));

    expect(window.location.hash).toBe("#/deployments/dep-instance-1");
    expect(await screen.findByRole("heading", { name: "Managed YAML Agent" })).toBeInTheDocument();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "详情" })).not.toBeInTheDocument();
    expect(screen.getAllByText("cloud-agent-1").length).toBeGreaterThanOrEqual(2);
    expect(screen.getAllByText("ar-cloud-ui").length).toBeGreaterThanOrEqual(2);
    expect(screen.getByText("http://ar-cloud-ui.example.test")).toBeInTheDocument();
    expect(await screen.findByRole("region", { name: "云端版本历史" })).toBeInTheDocument();
    expect(screen.getByText("cloud-agent-0")).toBeInTheDocument();
    expect(apiFetch).toHaveBeenCalledWith("/api/v1/cloud-agents/ar-cloud-ui/versions?page=1&size=100");
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

  it("exposes the Build deployment entry in the page header", async () => {
    const onSelectBuild = vi.fn();
    renderPage(vi.fn(), onSelectBuild);

    fireEvent.click(await screen.findByRole("button", { name: "选择 Build 部署" }));
    expect(onSelectBuild).toHaveBeenCalledOnce();
  });
});

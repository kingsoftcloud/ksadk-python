import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

const { apiFetch } = vi.hoisted(() => ({ apiFetch: vi.fn() }));
const defaultBuilds = [
  { id: "build-current", status: "SUCCEEDED" },
  { id: "build-previous", status: "SUCCEEDED" },
];
let agentBuilds: Array<{ id: string; status: string; createdAt?: string }> = defaultBuilds;
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
      versionId: "cloud-agent-1",
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
      versionId: "cloud-agent-1", status: "DEPLOYING",
      target: { region: "cn-beijing-6", environment: "preproduction" },
      agentId: "ar-cloud-ui", instanceId: "instance-1",
      endpoint: "http://ar-cloud-ui.example.test", artifactId: "managed-runtime",
    }));
  }
  if (path === "/api/v1/builds/build-current") return new Response(JSON.stringify({ agentId: "demo-agent" }));
  if (path === "/api/v1/agents/demo-agent") return new Response(JSON.stringify({
    builds: agentBuilds,
  }));
  if (path === "/api/v1/builds/build-latest/deployments") {
    expect(init?.method).toBe("POST");
    return new Response(JSON.stringify({ id: "operation-update" }));
  }
  if (path === "/api/v1/deployments/dep-instance-1:rollback") {
    expect(init?.method).toBe("POST");
    expect(JSON.parse(String(init?.body))).toEqual({ targetBuildId: "build-previous" });
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
    return new Response(JSON.stringify({ status: "SUCCEEDED", resourceId: "dep-instance-2" }));
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

  it("refreshes a Server-projected status and rolls back to an explicit historical Build", async () => {
    const user = userEvent.setup();
    renderPage();

    expect(await screen.findByText("ar-cloud-ui")).toBeInTheDocument();
    expect(screen.queryByText("preproduction")).not.toBeInTheDocument();
    expect(screen.queryByText("cn-beijing-6")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Managed YAML Agent 的更多操作" }));
    await user.click(await screen.findByRole("menuitem", { name: "刷新状态" }));
    await waitFor(() => expect(apiFetch).toHaveBeenCalledWith("/api/v1/deployments/dep-instance-1"));
    expect(await screen.findByText("部署中")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Managed YAML Agent 的更多操作" }));
    await user.click(await screen.findByRole("menuitem", { name: "选择回滚 Build" }));
    expect(await screen.findByLabelText("选择回滚目标 Build")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("选择回滚目标 Build"), { target: { value: "build-previous" } });
    fireEvent.click(screen.getByRole("button", { name: "提交回滚" }));
    await waitFor(() => expect(apiFetch).toHaveBeenCalledWith(
      "/api/v1/deployments/dep-instance-1:rollback",
      expect.objectContaining({ method: "POST" }),
    ));
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

  it("shows receipt-bound cloud details and local Build version history", async () => {
    const user = userEvent.setup();
    renderPage();

    await user.click(await screen.findByRole("button", { name: "查看 Managed YAML Agent 详情" }));

    expect(window.location.hash).toBe("#/deployments/dep-instance-1");
    expect(await screen.findByRole("heading", { name: "Managed YAML Agent" })).toBeInTheDocument();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "详情" })).not.toBeInTheDocument();
    expect(screen.getByText("cloud-agent-1")).toBeInTheDocument();
    expect(screen.getAllByText("ar-cloud-ui").length).toBeGreaterThanOrEqual(2);
    expect(screen.getByText("http://ar-cloud-ui.example.test")).toBeInTheDocument();
    expect(await screen.findByText("build-previous")).toBeInTheDocument();
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
    expect(await screen.findByText("无 Studio 部署记录")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "部署最新 Build" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "选择版本回滚" })).not.toBeInTheDocument();
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

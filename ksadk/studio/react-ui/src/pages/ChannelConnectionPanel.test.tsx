import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ChannelConnectionPanel } from "./ChannelConnectionPanel";

const { fetchMock } = vi.hoisted(() => ({ fetchMock: vi.fn() }));
vi.mock("../api", () => ({ apiFetch: fetchMock }));

const json = (value: unknown, status = 200) =>
  new Response(JSON.stringify(value), { status, headers: { "Content-Type": "application/json" } });

const emptyConnection = () => ({
  serverUrl: "", accountId: "", workspaceId: "", hasApiToken: false, agents: [],
});

beforeEach(() => {
  fetchMock.mockReset();
  fetchMock.mockResolvedValue(json(emptyConnection()));
});

describe("ChannelConnectionPanel", () => {
  it("shows an empty unconfigured state without example data or credentials", async () => {
    render(<ChannelConnectionPanel agents={[]} onWorkspace={vi.fn()} onChange={vi.fn()} />);
    await waitFor(() => expect(document.body.textContent).toContain("尚未配置"));
    expect(screen.getAllByDisplayValue("").length).toBeGreaterThan(0);
    expect(screen.queryByText(/example|fixture|token-demo/i)).not.toBeInTheDocument();
    expect(document.body.textContent).not.toContain("secret");
  });

  it("saves service and Agent connection fields while keeping returned credentials redacted", async () => {
    const user = userEvent.setup();
    const requests: Array<{ url: string; init: RequestInit }> = [];
    fetchMock.mockImplementation(async (url: string, init: RequestInit = {}) => {
      requests.push({ url, init });
      if (url.endsWith("/connection") && init.method === "PUT") {
        return json({ serverUrl: "https://channel.test", accountId: "acct", workspaceId: "ws", hasApiToken: true,
          agents: [{ agentId: "agent-1", enabled: false, hasToken: false, connected: false, state: "disabled" }] });
      }
      if (url.includes("/agents/agent-1/connection")) {
        return json({ serverUrl: "https://channel.test", accountId: "acct", workspaceId: "ws", hasApiToken: true,
          agents: [{ agentId: "agent-1", enabled: true, hasToken: true, connected: true, state: "connected" }] });
      }
      return json(emptyConnection());
    });
    render(<ChannelConnectionPanel agents={[{ metadata: { id: "agent-1", name: "研究 Agent" } }]} onWorkspace={vi.fn()} onChange={vi.fn()} />);
    await user.type(screen.getByLabelText("服务地址"), "https://channel.test");
    await user.type(screen.getByLabelText("账号 ID"), "acct");
    await user.type(screen.getByLabelText("工作区 ID"), "ws");
    await user.type(screen.getByLabelText("服务访问凭证"), "service-secret");
    await user.click(screen.getByRole("button", { name: "保存连接配置" }));
    await waitFor(() => expect(requests.some(request => request.init.method === "PUT")).toBe(true));
    const serviceRequest = requests.find(request => request.url.endsWith("/connection") && request.init.method === "PUT")!;
    expect(JSON.parse(String(serviceRequest.init.body))).toEqual({ serverUrl: "https://channel.test", accountId: "acct", workspaceId: "ws", apiToken: "service-secret" });

    await user.selectOptions(screen.getByLabelText("本地 Agent"), "agent-1");
    await user.type(screen.getByLabelText("Agent 连接凭证"), "agent-secret");
    await user.click(screen.getByRole("button", { name: "连接 Agent" }));
    await waitFor(() => expect(requests.some(request => request.url.includes("/agents/agent-1/connection"))).toBe(true));
    const agentRequest = requests.find(request => request.url.includes("/agents/agent-1/connection"))!;
    expect(JSON.parse(String(agentRequest.init.body))).toEqual({ enabled: true, token: "agent-secret" });
    expect(document.body.textContent).not.toContain("service-secret");
    expect(document.body.textContent).not.toContain("agent-secret");
  });
});

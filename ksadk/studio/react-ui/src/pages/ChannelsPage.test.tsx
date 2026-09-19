import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ChannelsPage } from "./ChannelsPage";

const { fetchMock, channelMock } = vi.hoisted(() => ({ fetchMock: vi.fn(), channelMock: vi.fn() }));
vi.mock("../api", () => ({ apiFetch: fetchMock }));
vi.mock("./channelApi", () => ({ channelApi: channelMock }));

const json = (value: unknown, status = 200) =>
  new Response(JSON.stringify(value), { status, headers: { "Content-Type": "application/json" } });

beforeEach(() => {
  fetchMock.mockReset();
  channelMock.mockReset();
  fetchMock.mockImplementation(async (url: string) => {
    if (url.includes("/connection")) return json({ serverUrl: "", accountId: "", workspaceId: "", hasApiToken: false, agents: [] });
    if (url.includes("/agents?")) return json({ items: [] });
    if (url.includes("/cloud-agents")) return json({ items: [] });
    if (url.includes("/runs")) return json({ items: [] });
    return json({});
  });
  channelMock.mockResolvedValue({ Items: [], Total: 0 });
});

describe("ChannelsPage empty-state behavior", () => {
  it("renders only persisted empty state and no seed channel data", async () => {
    render(<ChannelsPage />);
    expect(await screen.findByText("还没有渠道")).toBeInTheDocument();
    expect(screen.getByText("渠道总数").parentElement).toHaveTextContent("0");
    expect(screen.queryByText(/飞书示例|示例渠道|fixture|demo/i)).not.toBeInTheDocument();
  });

  it("surfaces backend failure without inventing channels", async () => {
    channelMock.mockRejectedValue(new Error("渠道服务暂不可用"));
    render(<ChannelsPage />);
    expect(await screen.findByRole("alert")).toHaveTextContent("渠道服务暂不可用");
    expect(screen.queryByText("飞书示例")).not.toBeInTheDocument();
  });

  it("keeps local and cloud agents distinct when their IDs collide", async () => {
    const user = userEvent.setup();
    const creates: Array<Record<string, unknown>> = [];
    fetchMock.mockImplementation(async (url: string) => {
      if (url.endsWith("/connection")) {
        return json({ serverUrl: "https://channel.test", accountId: "acct", workspaceId: "local-workspace", hasApiToken: true, agents: [] });
      }
      if (url.includes("/agents?")) return json({ items: [{ metadata: { id: "same-agent", name: "Local Agent" } }] });
      if (url.includes("/cloud-agents")) return json({ items: [{ agentId: "same-agent", name: "Cloud Agent" }] });
      if (url.includes("/runs")) return json({ items: [] });
      return json({});
    });
    channelMock.mockImplementation(async (action: string, body: Record<string, unknown> = {}) => {
      if (action === "CreateChannel") creates.push(body);
      return { Items: [], Total: 0 };
    });

    render(<ChannelsPage />);
    expect(await screen.findByText("还没有渠道")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "新建渠道" }));
    await user.click(screen.getByRole("combobox", { name: "绑定 Agent" }));

    const localOption = await screen.findByRole("option", { name: "本地 · Local Agent" });
    const cloudOption = screen.getByRole("option", { name: "云端 · Cloud Agent" });
    expect(localOption).toBeInTheDocument();
    expect(cloudOption).toBeInTheDocument();
    await user.click(localOption);
    expect(screen.getByRole("combobox", { name: "绑定 Agent" })).toHaveAttribute(
      "title",
      "本地 · Local Agent",
    );
    await user.type(screen.getByLabelText(/渠道账号 ID/), "local-account");
    await user.type(screen.getByLabelText(/App ID/), "local-app");
    await user.type(screen.getByLabelText(/App Secret/), "local-secret");
    await user.click(screen.getByRole("button", { name: "创建" }));
    await waitFor(() => expect(creates).toHaveLength(1));
    expect(creates[0]).toMatchObject({ AgentId: "same-agent", ConnectorWorkspaceId: "local-workspace" });

    await user.click(screen.getByRole("button", { name: "新建渠道" }));
    await user.click(screen.getByRole("combobox", { name: "绑定 Agent" }));
    await user.click(screen.getByRole("option", { name: "云端 · Cloud Agent" }));
    expect(screen.getByRole("combobox", { name: "绑定 Agent" })).toHaveAttribute(
      "title",
      "云端 · Cloud Agent",
    );
    await user.type(screen.getByLabelText(/渠道账号 ID/), "cloud-account");
    await user.type(screen.getByLabelText(/App ID/), "cloud-app");
    await user.type(screen.getByLabelText(/App Secret/), "cloud-secret");
    await user.click(screen.getByRole("button", { name: "创建" }));
    await waitFor(() => expect(creates).toHaveLength(2));
    expect(creates[1]).toMatchObject({ AgentId: "same-agent", ConnectorWorkspaceId: "" });
  });

  it("preserves a channel's connector workspace when editing an agent from another workspace", async () => {
    const user = userEvent.setup();
    const updates: Array<Record<string, unknown>> = [];
    fetchMock.mockImplementation(async (url: string) => {
      if (url.endsWith("/connection")) {
        return json({ serverUrl: "https://channel.test", accountId: "acct", workspaceId: "local-workspace", hasApiToken: true, agents: [] });
      }
      if (url.includes("/agents?")) return json({ items: [{ metadata: { id: "same-agent", name: "Local Agent" } }] });
      if (url.includes("/cloud-agents")) return json({ items: [{ agentId: "same-agent", name: "Cloud Agent" }] });
      if (url.includes("/runs")) return json({ items: [] });
      return json({});
    });
    channelMock.mockImplementation(async (action: string, body: Record<string, unknown> = {}) => {
      if (action === "ListChannels") {
        return {
          Items: [{
            Id: "channel-1",
            AgentId: "same-agent",
            ConnectorWorkspaceId: "other-workspace",
            Channel: "wps-xiezuo",
            ChannelAccountId: "account-1",
            Enabled: true,
            DmPolicy: "pairing",
            GroupPolicy: "allowlist",
            RequireMention: true,
            SessionScope: "per-peer",
            SecretRef: "secret",
            ConfigJson: "{}",
            CreatedBy: "test",
            CreatedAt: "2026-01-01T00:00:00Z",
            UpdatedAt: "2026-01-01T00:00:00Z",
          }],
          Total: 1,
        };
      }
      if (action === "UpdateChannel") {
        updates.push(body);
        return {};
      }
      return { Items: [], Total: 0 };
    });

    render(<ChannelsPage />);
    expect(await screen.findByRole("row", { name: "编辑渠道 account-1" })).toBeInTheDocument();
    await user.click(screen.getByRole("row", { name: "编辑渠道 account-1" }));
    expect(await screen.findByRole("dialog", { name: "编辑渠道" })).toBeInTheDocument();
    expect(screen.getByRole("combobox", { name: "绑定 Agent" })).toHaveAttribute(
      "title",
      "其他工作区 · same-agent",
    );

    await user.click(screen.getByRole("button", { name: "保存" }));
    await waitFor(() => expect(updates).toHaveLength(1));
    expect(updates[0]).toMatchObject({ AgentId: "same-agent", ConnectorWorkspaceId: "other-workspace" });
  });
});

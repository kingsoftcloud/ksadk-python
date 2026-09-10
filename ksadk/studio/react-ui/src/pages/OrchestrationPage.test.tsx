import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { apiFetch } from "../api";
import { OrchestrationPage } from "./OrchestrationPage";

vi.mock("../api", () => ({ apiFetch: vi.fn() }));

const mockedFetch = vi.mocked(apiFetch);

describe("OrchestrationPage graph canvas", () => {
  beforeEach(() => {
    mockedFetch.mockReset();
    mockedFetch.mockImplementation(async (input) => {
      const url = String(input);
      if (url.startsWith("/api/v1/catalog/resources")) {
        return { ok: true, json: async () => ({ items: [] }) } as Response;
      }
      if (url.startsWith("/api/v1/runs")) {
        return { ok: true, json: async () => ({ items: [] }) } as Response;
      }
      return {
        ok: true,
        json: async () => ({
          draft: {
            metadata: { id: "agentkit-a1b2c3d4", name: "Research", revision: 2 },
            spec: {
              runtime: { type: "codex" },
              execution: { strategy: "direct" },
              bindings: {
                modelProfileId: "glm-5.2",
                tools: ["search"],
                mcpServers: ["metaso"],
                skills: [],
              },
            },
          },
        }),
      } as Response;
    });
  });

  it("refreshes the displayed path and catalog without running the Agent", async () => {
    let tools = ["search"];
    mockedFetch.mockImplementation(async input => ({
      ok: true,
      json: async () => String(input) === "/api/v1/agents/demo-agent"
        ? { draft: { metadata: { id: "demo-agent", name: "Demo", revision: 1 }, spec: { runtime: { type: "codex" }, bindings: { tools } } } }
        : { items: [] },
    }) as Response);
    const props = { currentAgentId: "demo-agent", agents: [], onSelectAgent: vi.fn(), onCreate: vi.fn() };
    const view = render(<OrchestrationPage {...props} refreshTick={0} />);
    expect(await screen.findByText("1 个能力绑定")).toBeInTheDocument();
    mockedFetch.mockClear();
    tools = ["search", "read", "write"];
    view.rerender(<OrchestrationPage {...props} refreshTick={1} />);
    expect(await screen.findByText("3 个能力绑定")).toBeInTheDocument();
    expect(mockedFetch.mock.calls.map(([url]) => url).sort()).toEqual([
      "/api/v1/agents/demo-agent", "/api/v1/catalog/models", "/api/v1/catalog/resources?limit=200", "/api/v1/runs?limit=200",
    ]);
    expect(mockedFetch.mock.calls.every(([, init]) => !init?.method || init.method === "GET")).toBe(true);
  });

  it("renders the execution path in a compact clean canvas with fit controls", async () => {
    render(
      <OrchestrationPage
        currentAgentId="agentkit-a1b2c3d4"
        agents={[]}
        onSelectAgent={vi.fn()}
        onCreate={vi.fn()}
      />,
    );

    const canvas = await screen.findByRole("application", { name: "执行链路画布" });
    expect(canvas).toHaveAttribute("data-layout", "adaptive-serpentine");
    expect(canvas).toHaveAttribute("data-background", "plain");
    expect(screen.getByRole("button", { name: "适应画布" })).toBeVisible();
    expect(screen.getByText("任务输入")).toBeInTheDocument();
    expect(screen.getByText("2 个能力绑定")).toBeInTheDocument();
    await waitFor(() => expect(mockedFetch).toHaveBeenCalledTimes(4));
  });
  it("offers navigation to configuration and chat without pretending the preview is editable", async () => {
    const user = userEvent.setup();
    const onEdit = vi.fn();
    const onOpenChat = vi.fn();
    render(<OrchestrationPage currentAgentId="demo-agent" agents={[]} onSelectAgent={vi.fn()} onCreate={vi.fn()} onEdit={onEdit} onOpenChat={onOpenChat} />);
    await screen.findByRole("application", { name: "执行链路画布" });
    expect(document.querySelector(".react-flow__controls-interactive")).not.toBeInTheDocument();
    expect(document.querySelectorAll(".react-flow__node.draggable")).toHaveLength(0);
    await user.click(screen.getByRole("button", { name: "编辑配置" }));
    expect(onEdit).toHaveBeenCalledWith("demo-agent");
    await user.click(screen.getByRole("button", { name: "打开对话" }));
    expect(onOpenChat).toHaveBeenCalledTimes(1);
    expect(onOpenChat).toHaveBeenCalledWith();
    const configuration = screen.getByText("执行配置");
    expect(configuration.closest("details")).not.toHaveAttribute("open");
    await user.click(configuration);
    expect(screen.getByText("最大步骤")).toBeVisible();
    expect(screen.queryByText("未连接")).not.toBeInTheDocument();
  });

  it("shows failed loading as a retryable error instead of an empty Agent state", async () => {
    const user = userEvent.setup();
    const fetchSuccess = mockedFetch.getMockImplementation()!;
    mockedFetch.mockImplementation(async (input, init) => String(input).startsWith("/api/v1/agents/")
      ? { ok: false } as Response : fetchSuccess(input, init));
    render(<OrchestrationPage currentAgentId="demo-agent" agents={[]} onSelectAgent={vi.fn()} onCreate={vi.fn()} />);
    expect(await screen.findByRole("alert")).toHaveTextContent("Agent 配置加载失败");
    expect(screen.queryByRole("button", { name: "创建 Agent" })).not.toBeInTheDocument();
    mockedFetch.mockImplementation(fetchSuccess);
    await user.click(screen.getByRole("button", { name: "重新加载" }));
    expect(await screen.findByRole("application", { name: "执行链路画布" })).toBeVisible();
  });

});

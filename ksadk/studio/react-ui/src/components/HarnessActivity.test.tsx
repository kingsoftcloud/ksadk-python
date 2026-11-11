import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { apiFetch } from "../api";
import { ActivityRow, compactCommentary, HarnessActivity, type Activity } from "./HarnessActivity";

vi.mock("../api", () => ({ apiFetch: vi.fn() }));
afterEach(() => vi.resetAllMocks());
const row: Activity = { id: "child-1", label: "调研 Codex", status: "completed", kind: "subagent", details: [{ id: "overview", text: "已搜索 3 次资料，查看 2 个网页" }], children: [
  { id: "search", kind: "step", label: "搜索资料", status: "completed", details: [{ id: "query", text: "搜索资料：Codex 长任务" }] },
] };
describe("expandable human-friendly activity", () => {
  it.each([
    ["running", "正在压缩上下文"], ["completed", "上下文已压缩"],
    ["cancelled", "上下文压缩已取消"], ["failed", "上下文压缩失败"],
  ])("shows real compaction state %s without a generic execution disclosure", (status, text) => {
    const { container } = render(<HarnessActivity streaming={status === "running"}
      showExecutionDetails={false} fallback={[{
        id: "compact", kind: "step", category: "context", label: "压缩上下文",
        status, durationMs: 64000, details: [{ id: "detail", text: "host summary diagnostic" }],
      }]} />);
    expect(screen.getByText(text)).toBeVisible();
    expect(container).toHaveTextContent("1m04s");
    expect(container.querySelector("details, summary, .activity-chevron")).toBeNull();
    expect(container).not.toHaveTextContent("host summary diagnostic");
    expect(container.querySelector(".harness-context-activity")).toHaveAttribute("data-status", status);
  });
  it("does not append a second compaction row after a canonical timeline", () => {
    const { container } = render(<HarnessActivity canonicalTimeline streaming={false} fallback={[{
      id: "compact", kind: "step", category: "context", label: "压缩上下文", status: "completed", details: [],
    }]} />);
    expect(container.querySelector(".harness-context-activity")).toBeNull();
  });
  it("reports a curated plan to the session dock without rendering it in the body", async () => {
    const todos = [{ id: "build", content: "Build game", status: "in_progress", priority: "high" }];
    vi.mocked(apiFetch).mockResolvedValue(new Response(JSON.stringify({ runId: "run-plan", status: "interrupted", activities: [{
      id: "plan", kind: "step", label: "更新计划", status: "completed", details: [], tools: [{
        id: "tool-plan", callId: "call-plan", status: "completed", display: { version: "tool-display/v1", category: "todo", todos },
      }],
    }] })));
    const onPlan = vi.fn();
    const { container } = render(<HarnessActivity runId="run-plan" streaming={false} fallback={[]}
      canonicalTimeline showExecutionDetails={false} showPlan={false} onPlan={onPlan} />);
    await waitFor(() => expect(onPlan).toHaveBeenCalledWith("run-plan", todos));
    expect(container).not.toHaveTextContent("进程");
    expect(container).not.toHaveTextContent("执行记录");
  });
  it.each(["unverified", "partial", "blocked"])("never presents completed/%s as full task acceptance", async outcome => {
    vi.mocked(apiFetch).mockResolvedValue(new Response(JSON.stringify({
      runId: "run-unaccepted", status: "completed", activities: [], terminal: { status: "completed", outcome },
    })));
    const onRunState = vi.fn();
    render(<HarnessActivity runId="run-unaccepted" streaming={false} fallback={[]} onRunState={onRunState} />);
    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent("不能视为完整任务验收通过"));
    expect(screen.getByRole("status")).not.toHaveClass("harness-run-stopped");
    expect(onRunState).toHaveBeenCalledWith("run-unaccepted", { status: "completed", outcome });
  });
  it("keeps progress in the side dock and routine execution details out of the conversation", async () => {
    const dock = document.createElement("div"); document.body.append(dock);
    const tool = { id: "plan", callId: "plan-call", status: "completed", display: {
      version: "tool-display/v1" as const, category: "todo" as const,
      todos: [{ id: "build", content: "Build game", status: "in_progress" as const, priority: "high" }],
    } };
    const view = render(<HarnessActivity streaming fallback={[{
      id: "plan-step", label: "更新计划", kind: "step", status: "completed", details: [], tools: [tool],
    }]} canonicalTimeline showExecutionDetails={false} planContainer={dock} />);
    expect(view.container).not.toHaveTextContent("进程");
    expect(view.container).not.toHaveTextContent("执行记录");
    expect(dock).toHaveTextContent("进程0/1");
    const plan = dock.querySelector("details")!;
    expect(plan.open).toBe(false);
    await userEvent.setup().click(dock.querySelector("summary")!);
    expect(plan.open).toBe(true);
    expect(dock).toHaveTextContent("Build game");
    view.unmount(); expect(dock).toBeEmptyDOMElement(); dock.remove();
  });
  it("expands each child and its steps independently", async () => {
    const user = userEvent.setup();
    render(<><ActivityRow activity={row} /><ActivityRow activity={{ ...row, id: "child-2", label: "调研 ADK", status: "running" }} /></>);
    expect(screen.getAllByText("已搜索 3 次资料，查看 2 个网页")[0]).not.toBeVisible();
    await user.click(screen.getByText("调研 Codex"));
    expect(screen.getAllByText("已搜索 3 次资料，查看 2 个网页")[0]).toBeVisible();
    expect(screen.getAllByText("已搜索 3 次资料，查看 2 个网页")[1]).not.toBeVisible();
    await user.click(screen.getAllByText("搜索资料")[0]);
    expect(screen.getAllByText("搜索资料：Codex 长任务")[0]).toBeVisible();
  });
  it("keeps opened rows when status updates and does not invent missing details", async () => {
    const user = userEvent.setup();
    const { rerender } = render(<ActivityRow activity={{ ...row, status: "running", details: [], children: [] }} />);
    await user.click(screen.getByText("调研 Codex"));
    rerender(<ActivityRow activity={{ ...row, details: [], children: [] }} />);
    expect(screen.getByText("这段记录没有保存更细的执行步骤。")).toBeVisible();
    expect(screen.getByText("已完成")).toBeVisible();
  });
  it("loads curated run evidence, rejects unsafe action links", async () => {
    vi.mocked(apiFetch).mockResolvedValue(new Response(JSON.stringify({ runId: "run-1", status: "completed", activities: [row] })));
    render(<HarnessActivity runId="run-1" streaming={false} fallback={[]} />);
    expect(await screen.findByText("调研 Codex")).toBeVisible();
    expect(apiFetch).toHaveBeenCalledWith("/api/v1/runs/run-1/activities", expect.objectContaining({ signal: expect.any(AbortSignal) }));
    const { container } = render(<ActivityRow activity={{ ...row, children: [], details: [{ id: "bad", text: "查看资料", href: "javascript:alert(1)" }] }} />);
    expect(container.querySelector("a")).toBeNull();
  });
  it("interleaves real public commentary without UI-authored filler", () => {
    const activities: Activity[] = [
      { id: "analysis", kind: "step", label: "分析任务", status: "completed", details: [] },
      { id: "plan", kind: "commentary", label: "公开进展", status: "completed", text: "我先分别核对三款产品的官方资料。", details: [] },
      { ...row, id: "codex", label: "调研 Codex", status: "completed" },
      { id: "codex-result", kind: "commentary", label: "Codex 的结论", status: "completed", text: "Codex 的长任务资料已核对，关键依据来自官方文档。", details: [] },
      { ...row, id: "adk", label: "调研 ADK", status: "completed" },
      { id: "compact", kind: "step", label: "压缩上下文", status: "completed", details: [] },
      { id: "summary", kind: "step", label: "汇总结果", status: "running", details: [] },
    ];
    const { container } = render(<HarnessActivity streaming fallback={activities} />);
    const text = container.textContent || "";
    const expected = [
      "我先分别核对三款产品的官方资料。",
      "调研 Codex",
      "Codex 的长任务资料已核对，关键依据来自官方文档。",
      "调研 ADK",
      "上下文已压缩",
      "汇总结果",
    ];
    expect(expected.every(value => text.includes(value))).toBe(true);
    expect(expected.map(value => text.indexOf(value))).toEqual(
      [...expected].map(value => text.indexOf(value)).sort((left, right) => left - right),
    );
    expect(screen.queryByText("分析任务")).not.toBeInTheDocument();
    expect(screen.getByText("Codex 的结论", { exact: false })).toBeVisible();
    expect(text).not.toContain("个子智能体已完成");
    expect(text).not.toContain("下面是最终汇总");
  });
  it("keeps routine tool bookkeeping behind one execution disclosure", async () => {
    const user = userEvent.setup();
    render(<HarnessActivity streaming={false} fallback={[
      { id: "think", kind: "step", category: "reasoning", label: "思考", status: "completed", details: [] },
      { id: "read", kind: "step", category: "tool", label: "读取文件", status: "completed", count: 34, details: [] },
      { id: "mcp", kind: "step", category: "tool", label: "调用 MCP 工具", status: "completed", count: 2, details: [] },
    ]} />);
    expect(screen.queryByText("思考")).not.toBeInTheDocument();
    expect(screen.getByText("读取文件、调用 MCP 工具 · 36 项")).toBeVisible();
    expect(screen.getByText("读取文件", { exact: true })).not.toBeVisible();
    await user.click(screen.getByText("执行记录"));
    expect(screen.getByText("读取文件", { exact: true })).toBeVisible();
  });
  it("deduplicates near-identical progress and keeps only a short milestone trail", () => {
    const commentary = [
      "先检查工作区和已绑定资源。",
      "已找到知识库，继续核对验收约束。",
      "知识库检索完成，已确认种子、存档版本和离线约束，现在开始并行委派三个子任务。",
      "知识库检索完成，关键约束已确认：种子、存档版本和离线约束。现在并行委派三个子任务。",
      "三个子任务已返回，开始整合结果。",
      "测试已通过，正在整理最终结果。",
    ].map((text, index): Activity => ({
      id: `progress-${index}`, kind: "commentary", label: "公开进展", status: "completed", text, details: [],
    }));
    const compacted = compactCommentary(commentary);
    expect(compacted).toHaveLength(4);
    expect(compacted[0].text).toBe("先检查工作区和已绑定资源。");
    expect(compacted.at(-1)?.text).toBe("测试已通过，正在整理最终结果。");
    expect(compacted.some(item => item.text === "知识库检索完成，已确认种子、存档版本和离线约束，现在开始并行委派三个子任务。")).toBe(false);
  });
  it("does not retain another run's evidence when changing conversations", async () => {
    vi.mocked(apiFetch).mockResolvedValueOnce(new Response(JSON.stringify({ runId: "run-1", status: "completed", activities: [row] }))).mockResolvedValueOnce(new Response("", { status: 404 }));
    const { rerender } = render(<HarnessActivity runId="run-1" streaming={false} fallback={[]} />);
    expect(await screen.findByText("调研 Codex")).toBeVisible();
    rerender(<HarnessActivity runId="run-2" streaming={false} fallback={[{ ...row, id: "fallback", label: "分析任务", details: [], children: [] }]} />);
    await waitFor(() => expect(screen.queryByText("调研 Codex")).not.toBeInTheDocument());
    expect(await screen.findByText(/详细步骤暂时无法加载/)).toBeVisible();
  });
  it("shows derived duration and opens the subagent detail panel", async () => {
    const user = userEvent.setup();
    const openDetail = vi.fn();
    render(<ActivityRow activity={{ ...row, durationMs: 18000, callId: "call-1", provider: "Codex" }} onOpenDetail={openDetail} />);
    expect(screen.getByText("18s")).toBeVisible();
    await user.click(screen.getByRole("button", { name: /调研 Codex/ }));
    expect(openDetail).toHaveBeenCalledWith(expect.objectContaining({ callId: "call-1" }));
  });
  it("identifies observed timing for both activity rows and child detail", async () => {
    vi.mocked(apiFetch).mockImplementation(async (input: RequestInfo | URL) => {
      const child = {
        ...row, callId: "call-1", durationMs: 18000, durationSource: "studio_observed",
      };
      return new Response(JSON.stringify(String(input).endsWith("/subagents/call-1") ? {
        ...child, runId: "run-1", parentStatus: "completed", facts: [],
      } : { runId: "run-1", status: "completed", activities: [child] }));
    });
    render(<HarnessActivity runId="run-1" streaming={false} fallback={[]} />);
    expect(await screen.findByText("18s")).toHaveAttribute(
      "title", "按记录到的事件时间计算，包含传输与排队时间。",
    );
    await userEvent.setup().click(screen.getByRole("button", { name: /调研 Codex/ }));
    await waitFor(() => expect(screen.getAllByText("18s")).toHaveLength(2));
  });
  it("distinguishes a partial tool group from a failed subagent", () => {
    render(<ActivityRow activity={{ ...row, kind: "step", label: "搜索资料", status: "partial" }} />);
    expect(screen.getByText("部分未完成")).toBeVisible();
    expect(screen.queryByText("执行失败")).not.toBeInTheDocument();
  });
  it("renders subagent detail facts from the curated detail endpoint", async () => {
    vi.mocked(apiFetch).mockImplementation(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith("/subagents/call-1")) {
        return new Response(JSON.stringify({
          runId: "run-1", callId: "call-1", label: "调研 Codex", status: "completed",
          durationMs: 18000, provider: "Codex", models: ["gpt-test"], parentStatus: "completed",
          facts: [{ id: "g1", kind: "step", label: "搜索资料", status: "completed", details: [{ id: "d1", text: "已完成：第 1 次搜索资料" }] }],
        }));
      }
      return new Response(JSON.stringify({ runId: "run-1", status: "completed", activities: [{ ...row, callId: "call-1", durationMs: 18000 }] }));
    });
    render(<HarnessActivity runId="run-1" streaming={false} fallback={[]} />);
    await userEvent.setup().click(await screen.findByRole("button", { name: /调研 Codex/ }));
    expect(await screen.findByText("进展摘要")).toBeVisible();
    expect(screen.getAllByText("18s").length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText("Provider：Codex")).toBeVisible();
    expect(screen.getByText("模型：gpt-test")).toBeVisible();
    // 事实行默认折叠在 <details> 内，展开后才可见。
    expect(screen.getByText("已完成：第 1 次搜索资料")).toBeInTheDocument();
    expect(screen.queryByText("打开完整子会话")).not.toBeInTheDocument();
  });
});

describe("G7 sandbox activity badge", () => {
  it("renders sandbox backend id and capabilities summary", async () => {
    const sandboxActivity: Activity & { capabilities?: Record<string, unknown>; rejected?: boolean } = {
      id: "run-1:sandbox:evt-1", kind: "sandbox", label: "沙箱执行 · local-os", status: "completed",
      details: [], capabilities: { filesystem_isolation: "scoped_workspace", network_control: "command_surface", process_boundary: false },
      rejected: false,
    };
    const activities: Activity[] = [sandboxActivity];
    vi.mocked(apiFetch).mockResolvedValueOnce(new Response(JSON.stringify({ runId: "run-1", status: "completed", activities })));
    render(<HarnessActivity runId="run-1" streaming={false} fallback={[]} />);
    await userEvent.setup().click(await screen.findByText("执行记录"));
    await userEvent.setup().click(screen.getByText("沙箱执行 · local-os"));
    // 成功沙箱归入执行记录，能力摘要仍可按需查看。
    expect(screen.getByText(/无进程边界/)).toBeVisible();
    expect(screen.getByText(/scoped_workspace/)).toBeVisible();
  });

  it("marks rejected sandbox execution as failed with rejection note", async () => {
    const sandboxActivity: Activity & { capabilities?: Record<string, unknown>; rejected?: boolean } = {
      id: "run-1:sandbox:evt-2", kind: "sandbox", label: "沙箱执行 · local-os", status: "failed",
      details: [], capabilities: { filesystem_isolation: "scoped_workspace", network_control: "command_surface", process_boundary: false },
      rejected: true,
    };
    vi.mocked(apiFetch).mockResolvedValueOnce(new Response(JSON.stringify({ runId: "run-1", status: "completed", activities: [sandboxActivity] })));
    render(<HarnessActivity runId="run-1" streaming={false} fallback={[]} />);
    expect(await screen.findByText("沙箱拒绝执行")).toBeInTheDocument();
    expect(screen.getByText("执行失败")).toBeInTheDocument();
  });
});

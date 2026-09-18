import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { apiFetch } from "../api";
import { ActivityRow, HarnessActivity, type Activity } from "./HarnessActivity";

vi.mock("../api", () => ({ apiFetch: vi.fn() }));
afterEach(() => vi.resetAllMocks());
const row: Activity = { id: "child-1", label: "调研 Codex", status: "completed", kind: "subagent", details: [{ id: "overview", text: "已搜索 3 次资料，查看 2 个网页" }], children: [
  { id: "search", kind: "step", label: "搜索资料", status: "completed", details: [{ id: "query", text: "搜索资料：Codex 长任务" }] },
] };
describe("expandable human-friendly activity", () => {
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
      "压缩上下文",
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
    await user.click(screen.getByRole("button", { name: "详情" }));
    expect(openDetail).toHaveBeenCalledWith(expect.objectContaining({ callId: "call-1" }));
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
    await userEvent.setup().click(await screen.findByRole("button", { name: "详情" }));
    expect(await screen.findByText("执行事实")).toBeVisible();
    expect(screen.getAllByText("18s").length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText("Provider：Codex")).toBeVisible();
    expect(screen.getByText("模型：gpt-test")).toBeVisible();
    // 事实行默认折叠在 <details> 内，展开后才可见。
    expect(screen.getByText("已完成：第 1 次搜索资料")).toBeInTheDocument();
    expect(screen.queryByText("打开完整子会话")).not.toBeInTheDocument();
  });
});

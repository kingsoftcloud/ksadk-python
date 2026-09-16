import { act, fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { CompactHarnessTimeline } from "./CompactHarnessTimeline";

vi.mock("@kingsoftcloud/ksadk-web/chat/timeline", () => ({ ChatMessageList: ({ messages }: { messages: unknown[] }) => <div data-testid="shared-message">{JSON.stringify(messages)}</div>, StatusBanner: () => <div data-testid="status-banner" /> }));
vi.mock("./HarnessActivity", () => ({ HarnessActivity: ({ runId, fallback }: { runId: string; fallback?: Array<{ label: string }> }) => <div data-testid="activity" data-fallback={fallback?.[0]?.label}>{runId}</div> }));
const props = { agentName: "Harness", isMobile: false, isStreaming: false, activity: null, sessionId: "ses-1",
  onDeleteFeedback: vi.fn(), onSubmitFeedback: vi.fn(), onRespondToApproval: vi.fn() };

describe("Harness timeline integration", () => {
  it("shows thinking only before any visible progress; existing history defers to the facade thinking strip", () => {
    // 有历史模型输出时 facade 已渲染流内"正在思考"折叠条，时间线不得再
    // 渲染第二个指示（双"正在思考"）。
    const messages = [
      { id: "old", role: "model" as const, timestamp: 1, content: "上轮回复", invocationId: "run-old" },
      { id: "user", role: "user" as const, timestamp: 2, content: "继续" },
      { id: "pending", role: "model" as const, timestamp: 3, content: "", eventType: "optimistic_assistant_placeholder" },
    ];
    const { rerender } = render(<CompactHarnessTimeline {...props} isStreaming messages={messages} />);
    expect(screen.queryByRole("status")).toBeNull();

    // 全新会话：无任何模型输出时显示流光；首个增量到达后消失。
    const fresh = [
      { id: "user", role: "user" as const, timestamp: 2, content: "继续" },
      { id: "pending", role: "model" as const, timestamp: 3, content: "", eventType: "optimistic_assistant_placeholder" },
    ];
    rerender(<CompactHarnessTimeline {...props} isStreaming messages={fresh} />);
    const thinking = screen.getByRole("status");
    expect(thinking).toHaveTextContent("正在思考…");
    expect(thinking.querySelector(".text-shimmer")).not.toBeNull();
    rerender(<CompactHarnessTimeline {...props} isStreaming messages={[
      ...fresh.slice(0, -1), { ...fresh[1], content: "首个增量", invocationId: "run-new" },
    ]} />);
    expect(screen.queryByText("正在思考…")).toBeNull();
  });
  it("clears the waiting state when cancelled without a token", () => {
    const { rerender } = render(<CompactHarnessTimeline {...props} isStreaming messages={[]} emptyState={<p>欢迎</p>} />);
    expect(screen.getByText("正在思考…")).toBeInTheDocument();
    expect(screen.queryByText("欢迎")).toBeNull();
    rerender(<CompactHarnessTimeline {...props} messages={[]} emptyState={<p>欢迎</p>} />);
    expect(screen.queryByText("正在思考…")).toBeNull();
    expect(screen.getByText("欢迎")).toBeInTheDocument();
  });
  it("shows live execution activity before the first public model message", () => {
    render(<CompactHarnessTimeline {...props} isStreaming activeRunId="run-live" messages={[
      { id: "user", role: "user", timestamp: 1, content: "执行一个长任务" },
      { id: "pending", role: "model", timestamp: 2, content: "", eventType: "optimistic_assistant_placeholder" },
    ]} />);
    expect(screen.getByTestId("activity")).toHaveTextContent("run-live");
    expect(screen.getByTestId("activity")).toHaveAttribute("data-fallback", "分析任务");
    expect(screen.queryByText("正在思考…")).toBeNull();
  });
  it("does not duplicate the live activity after a public model item arrives", () => {
    render(<CompactHarnessTimeline {...props} isStreaming activeRunId="run-live" messages={[
      { id: "user", role: "user", timestamp: 1, content: "执行一个长任务" },
      { id: "progress", role: "model", timestamp: 2, content: "", reasoning: "正在分析任务", invocationId: "run-live" },
    ]} />);
    expect(screen.getAllByTestId("activity")).toHaveLength(1);
  });
  it("uses real pagination state and keeps a single history control before all messages", () => {
    const messages = [{ id: "user", role: "user" as const, timestamp: 1, content: "当前消息" }];
    const { container, rerender } = render(<CompactHarnessTimeline {...props} messages={messages} hasMoreMessages onLoadOlderSessionMessages={vi.fn()} />);
    expect(screen.getByRole("button", { name: "加载更早消息" }).closest(".harness-history-loader"))
      .toBe(container.firstElementChild?.firstElementChild);
    rerender(<CompactHarnessTimeline {...props} messages={Array.from({ length: 60 }, (_, index) => ({ ...messages[0], id: String(index) }))} hasMoreMessages={false} onLoadOlderSessionMessages={vi.fn()} />);
    expect(screen.queryByRole("button", { name: "加载更早消息" })).toBeNull();
  });
  it("preserves the reading position when older messages are prepended and blocks duplicate requests", async () => {
    let finish!: () => void;
    const load = vi.fn(() => new Promise<void>(resolve => { finish = resolve; }));
    const messages = [{ id: "current", role: "user" as const, timestamp: 2, content: "当前消息" }];
    const { container, rerender } = render(<CompactHarnessTimeline {...props} messages={messages} hasMoreMessages onLoadOlderSessionMessages={load} />);
    const scroller = container.firstElementChild as HTMLElement;
    Object.defineProperty(scroller, "scrollHeight", { configurable: true, value: 1000 });
    Object.defineProperty(scroller, "clientHeight", { configurable: true, value: 400 });
    scroller.scrollTop = 50;
    fireEvent.scroll(scroller);
    fireEvent.click(screen.getByRole("button", { name: "加载更早消息" }));
    fireEvent.click(screen.getByRole("button", { name: "正在加载更早消息…" }));
    expect(load).toHaveBeenCalledTimes(1);
    Object.defineProperty(scroller, "scrollHeight", { configurable: true, value: 1400 });
    rerender(<CompactHarnessTimeline {...props} messages={[{ ...messages[0], id: "older", timestamp: 1 }, ...messages]} hasMoreMessages onLoadOlderSessionMessages={load} />);
    expect(scroller.scrollTop).toBe(450);
    await act(async () => finish());
    expect(screen.getByRole("button", { name: "加载更早消息" })).toBeEnabled();
  });
  it("retains the host timeline layout class after the master shell merge", () => {
    const { container } = render(<CompactHarnessTimeline {...props} messages={[]} className="studio-chat-timeline" />);
    expect(container.firstElementChild).toHaveClass("harness-timeline", "studio-chat-timeline");
  });
  it("has one expandable activity panel per run even across system or interactive boundaries", () => {
    render(<CompactHarnessTimeline {...props} messages={[
      { id: "m1", role: "model", timestamp: 1, content: "", invocationId: "run-a", reasoning: "正在分析任务" },
      { id: "sys", role: "system", timestamp: 2, content: "上下文已自动压缩" },
      { id: "m2", role: "model", timestamp: 3, content: "完成", invocationId: "run-a" },
      { id: "m3", role: "model", timestamp: 4, content: "第二轮完成", invocationId: "run-b" },
    ]} />);
    expect(screen.getAllByTestId("activity").map(element => element.textContent)).toEqual(["run-a", "run-b"]);
    expect(screen.getAllByTestId("status-banner")).toHaveLength(1);
  });
  it("retains shared approval and final answer rendering", () => {
    render(<CompactHarnessTimeline {...props} messages={[
      { id: "m1", role: "model", timestamp: 1, content: "", invocationId: "run-a",
        tools: { write: { name: "write_workspace_file", args: "report.md", status: "paused", approvalRequestId: "approval-1" } } },
      { id: "m2", role: "model", timestamp: 2, content: "报告已保存", invocationId: "run-a" },
    ]} />);
    expect(screen.getByTestId("shared-message")).toHaveTextContent("approval-1");
    expect(screen.getByTestId("shared-message")).toHaveTextContent("报告已保存");
  });
  it("keeps structured interactive surfaces even with no answer text, without raw reasoning", () => {
    render(<CompactHarnessTimeline {...props} messages={[
      { id: "form", role: "model", timestamp: 1, content: "", reasoning: "private reasoning", invocationId: "run-a",
        aguiActivities: [{ surfaceId: "form-1", messages: [] }] },
    ]} />);
    expect(screen.getByTestId("shared-message")).toHaveTextContent("form-1");
    expect(screen.getByTestId("shared-message")).not.toHaveTextContent("private reasoning");
  });
});

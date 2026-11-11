import { act, fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { CompactHarnessTimeline } from "./CompactHarnessTimeline";

vi.mock("@kingsoftcloud/ksadk-web/chat/timeline", () => ({ ChatMessageList: ({ messages, renderProcessingBlock, showAgentHeader }: any) => <div data-testid="shared-message" data-agent-header={String(showAgentHeader)}>{JSON.stringify(messages)}{messages.flatMap((message: any) => (message.blocks || []).map((block: any) => renderProcessingBlock?.(block)))}</div>, StatusBanner: () => <div data-testid="status-banner" /> }));
const planCallbacks = vi.hoisted(() => new Map<string, (runId: string, items: any[]) => void>());
const stateCallbacks = vi.hoisted(() => new Map<string, (runId: string, state: any) => void>());
const runCallbacks = vi.hoisted(() => new Map<string, (reference: string, runId: string) => void>());
vi.mock("./HarnessActivity", async importOriginal => ({ ...await importOriginal<typeof import("./HarnessActivity")>(), HarnessActivity: ({ runId, fallback, onPlan, onRunState, onResolvedRun, visibleThinking }: { runId: string; fallback?: Array<{ label: string }>; onPlan?: (runId: string, items: any[]) => void; onResolvedRun?: (reference: string, runId: string) => void; onRunState?: (runId: string, state: any) => void; visibleThinking?: boolean }) => {
  if (onPlan) planCallbacks.set(runId, onPlan);
  if (onRunState) stateCallbacks.set(runId, onRunState);
  if (onResolvedRun) runCallbacks.set(runId, onResolvedRun);
  return <div data-testid="activity" data-fallback={fallback?.[0]?.label} data-visible-thinking={String(visibleThinking)}>{runId}</div>;
} }));
vi.mock("./RunElapsed", () => ({ RunElapsed: ({ runId }: { runId: string }) => <div data-testid="run-clock" data-run-id={runId} /> }));
const props = { agentName: "Harness", isMobile: false, isStreaming: false, activity: null, sessionId: "ses-1",
  onDeleteFeedback: vi.fn(), onSubmitFeedback: vi.fn(), onRespondToApproval: vi.fn() };

describe("Harness timeline integration", () => {
  it("recognizes a canonical conversation item after the shared live state loses run metadata", () => {
    const { container } = render(<CompactHarnessTimeline {...props} isStreaming activeRunId="run-current" messages={[
      { id: "user", role: "user", timestamp: 1, content: "Build a platformer" },
      { id: "conversation:run-current:item-thought", role: "model", timestamp: 2, content: "",
        blocks: [{ id: "conversation-block:item-thought", type: "thinking", source: "provider_reasoning",
          status: "streaming", content: "Designing the level" }] },
    ]} />);
    expect(container.querySelector(".harness-live-run")).toBeNull();
    expect(screen.getAllByTestId("run-clock")).toHaveLength(1);
    expect(screen.getByTestId("run-clock").parentElement?.firstElementChild).toBe(screen.getByTestId("run-clock"));
    expect(screen.getAllByTestId("activity")).toHaveLength(1);
    expect(screen.getByTestId("activity")).toHaveAttribute("data-visible-thinking", "true");
  });
  it("keeps metadata-free canonical items from distinct runs separate before compaction", () => {
    render(<CompactHarnessTimeline {...props} messages={[
      { id: "conversation:run-first:item-a", role: "model", timestamp: 1, content: "First result" },
      { id: "conversation:run-second:item-b", role: "model", timestamp: 2, content: "Second result" },
    ]} />);
    expect(screen.getAllByTestId("run-clock").map(node => node.dataset.runId)).toEqual(["run-first", "run-second"]);
    expect(screen.getAllByTestId("shared-message")).toHaveLength(2);
  });
  it("keeps a compaction boundary between the work that precedes and follows it", () => {
    const { container, rerender } = render(<CompactHarnessTimeline {...props} messages={[
      { id: "before", role: "model", timestamp: 1, content: "Before", runId: "run-a" },
      { id: "context", role: "system", timestamp: 2, content: "上下文已压缩", runId: "run-a",
        contextActivity: { status: "completed", durationMs: 19000 } },
      { id: "after", role: "model", timestamp: 3, content: "After", runId: "run-a" },
    ]} />);
    expect([...container.querySelectorAll(".harness-turn[data-message-id]")].map(node => node.getAttribute("data-message-id")))
      .toEqual(["before", "context", "after"]);
    const row = container.querySelector('[data-message-id="context"]')!;
    expect(row).toHaveTextContent("上下文已压缩19s");
    expect(row.querySelector("details, summary")).toBeNull();
    expect(screen.getAllByTestId("run-clock")).toHaveLength(1);
    rerender(<CompactHarnessTimeline {...props} messages={[
      { id: "before", role: "model", timestamp: 1, content: "Before", runId: "run-a" },
      { id: "context", role: "system", timestamp: 2, content: "上下文压缩已取消", runId: "run-a", contextActivity: { status: "cancelled" } },
      { id: "after", role: "model", timestamp: 3, content: "After", runId: "run-a" },
    ]} />);
    expect(row).toHaveTextContent("上下文压缩已取消");
    expect(row).not.toHaveTextContent("上下文已压缩");
  });
  it("unifies approval response aliases with their host run and keeps one clock and thinking row", () => {
    const { rerender } = render(<CompactHarnessTimeline {...props} isStreaming activeRunId="run-parent" messages={[
      { id: "user", role: "user", timestamp: 1, content: "Build game" },
      { id: "m1", role: "model", timestamp: 2, content: "Before approval", invocationId: "run-parent" },
      { id: "approval", role: "model", timestamp: 3, content: "", invocationId: "run-response", aguiActivities: [{ surfaceId: "approval", messages: [] }] },
      { id: "m2", role: "model", timestamp: 4, content: "", invocationId: "run-response", blocks: [
        { id: "thought", type: "thinking", source: "provider_reasoning", status: "streaming", content: "Inspecting current files" },
      ] },
    ]} />);
    act(() => runCallbacks.get("run-response")!("run-response", "run-parent"));
    expect(screen.getAllByTestId("run-clock")).toHaveLength(1);
    expect(screen.getAllByTestId("activity")).toHaveLength(1);
    expect(screen.getByTestId("activity")).toHaveAttribute("data-visible-thinking", "true");
    const old = runCallbacks.get("run-response")!;
    rerender(<CompactHarnessTimeline {...props} sessionId="another-session" messages={[]} />);
    act(() => old("run-response", "run-parent"));
    expect(screen.queryByTestId("activity")).toBeNull();
  });
  it("places one work header before the first assistant output of each run", () => {
    const { container } = render(<CompactHarnessTimeline {...props} messages={[
      { id: "user", role: "user", timestamp: 1, content: "Build game" },
      { id: "first", role: "model", timestamp: 2, content: "Planning", invocationId: "run-a" },
      { id: "notice", role: "system", timestamp: 3, content: "Checkpoint" },
      { id: "second", role: "model", timestamp: 4, content: "Writing files", invocationId: "run-a" },
      { id: "question", role: "user", timestamp: 5, content: "Where is the file?" },
      { id: "reply", role: "model", timestamp: 6, content: "Reply", invocationId: "run-b" },
    ]} />);
    const headers = screen.getAllByTestId("run-clock");
    expect(headers.map(node => node.dataset.runId)).toEqual(["run-a", "run-b"]);
    for (const header of headers) {
      expect(header.parentElement?.firstElementChild).toBe(header);
      expect(header.nextElementSibling).toHaveAttribute("data-testid", "shared-message");
    }
    expect(container.querySelector('[data-message-id="user"] [data-testid="run-clock"]')).toBeNull();
  });
  it("keeps receipt-only plans ordered by transcript and rejects stale session responses", () => {
    const messages = [
      { id: "older", role: "model" as const, timestamp: 1, content: "old", invocationId: "run-older" },
      { id: "newer", role: "model" as const, timestamp: 2, content: "new", invocationId: "run-newer" },
      { id: "follow-up", role: "model" as const, timestamp: 3, content: "reply", invocationId: "run-follow-up" },
    ];
    const { container, rerender } = render(<CompactHarnessTimeline {...props} messages={messages} />);
    const item = (content: string) => [{ id: content, content, status: "in_progress", priority: "high" }];
    act(() => planCallbacks.get("run-newer")!("run-newer", item("Newer plan")));
    const oldSessionCallback = planCallbacks.get("run-older")!;
    act(() => oldSessionCallback("run-older", item("Older plan")));
    expect(container.querySelector(".harness-plan-dock")).toHaveTextContent("Newer plan");
    expect(container.querySelector(".harness-plan-dock")).not.toHaveTextContent("Older plan");
    rerender(<CompactHarnessTimeline {...props} sessionId="ses-2" messages={messages} />);
    act(() => oldSessionCallback("run-older", item("Stale plan")));
    expect(container.querySelector(".harness-plan-dock")).toBeEmptyDOMElement();
  });
  it("keeps terminal plan ownership across follow-ups and ignores late states from another session", async () => {
    const messages = [
      { id: "game", role: "model" as const, timestamp: 1, content: "Game", runId: "run-game" },
      { id: "check", role: "model" as const, timestamp: 2, content: "UI check done", runId: "run-check" },
    ];
    const { container, rerender } = render(<CompactHarnessTimeline {...props} messages={messages} />);
    act(() => planCallbacks.get("run-game")!("run-game", [
      { id: "start", content: "Start game", status: "completed", priority: "high" },
      { id: "play", content: "Play game", status: "in_progress", priority: "high" },
    ]));
    act(() => stateCallbacks.get("run-game")!("run-game", { status: "completed", outcome: "unverified" }));
    act(() => stateCallbacks.get("run-check")!("run-check", { status: "completed", outcome: "verified" }));
    const dock = container.querySelector(".harness-plan-dock")!;
    expect(dock).toHaveTextContent("进程1/2");
    expect(dock).toHaveTextContent("前轮计划");
    expect(dock).toHaveTextContent("该轮已结束");
    expect(dock).toHaveTextContent("验收尚未确认");
    expect(dock).not.toHaveTextContent("验收通过");
    await userEvent.setup().click(dock.querySelector("summary")!);
    expect(dock).toHaveTextContent("结束前进行中");
    const oldState = stateCallbacks.get("run-game")!;
    rerender(<CompactHarnessTimeline {...props} sessionId="other-session" messages={[]} />);
    act(() => oldState("run-game", { status: "completed", outcome: "verified" }));
    expect(dock).toBeEmptyDOMElement();
  });
  it("keeps the latest recorded plan in the side dock after a follow-up without a new plan", () => {
    const { container } = render(<CompactHarnessTimeline {...props} messages={[
      { id: "plan-model", role: "model", timestamp: 1, content: "", invocationId: "run-plan", blocks: [
        { id: "plan-call", type: "tool", toolName: "TodoWrite", status: "completed", args: "", extra: { toolDisplay: {
          version: "tool-display/v1", category: "todo", todos: [
            { id: "render", content: "Build renderer", status: "in_progress", priority: "high" },
          ],
        } } },
      ] },
      { id: "reply-model", role: "model", timestamp: 2, content: "当前任务未完成", invocationId: "run-reply" },
    ]} />);
    expect(container.querySelector(".harness-plan-dock")).toHaveTextContent("进程0/1");
    expect(container.querySelector(".harness-timeline .harness-task-plan")).toBeNull();
  });
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
    const history = screen.getByRole("button", { name: "加载更早消息" }).closest(".harness-history-loader")!;
    expect(history.compareDocumentPosition(container.querySelector(".harness-turn")!) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    rerender(<CompactHarnessTimeline {...props} messages={Array.from({ length: 60 }, (_, index) => ({ ...messages[0], id: String(index) }))} hasMoreMessages={false} onLoadOlderSessionMessages={vi.fn()} />);
    expect(screen.queryByRole("button", { name: "加载更早消息" })).toBeNull();
  });
  it("preserves the reading position when older messages are prepended and blocks duplicate requests", async () => {
    let finish!: () => void;
    const load = vi.fn(() => new Promise<void>(resolve => { finish = resolve; }));
    const messages = [{ id: "current", role: "user" as const, timestamp: 2, content: "当前消息" }];
    const { container, rerender } = render(<CompactHarnessTimeline {...props} messages={messages} hasMoreMessages onLoadOlderSessionMessages={load} />);
    const scroller = container.querySelector(".harness-timeline") as HTMLElement;
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
    expect(container.querySelector(".harness-timeline")).toHaveClass("studio-chat-timeline");
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
  it("streams selected provider text with a rolling preview and retains manual expansion", () => {
    const live = [
      { id: "user", role: "user" as const, timestamp: 1, content: "问题" },
      { id: "live", role: "model" as const, timestamp: 2, content: "", invocationId: "run-live",
        blocks: [{ id: "t1", type: "thinking" as const, source: "provider_reasoning" as const, content: "第一步：读取工作区", status: "streaming" as const }] },
    ];
    const { rerender } = render(<CompactHarnessTimeline {...props} isStreaming messages={live} />);
    const block = screen.getByTestId("live-thinking");
    expect(block).toHaveAttribute("data-streaming", "true");
    expect(block).toHaveTextContent("第一步：读取工作区");
    expect(screen.getByRole("status")).toHaveTextContent("正在思考");
    expect(screen.queryByTestId("live-thinking-text")).toBeNull();
    const details = screen.getByRole("status").closest("details")!;
    details.open = true; fireEvent(details, new Event("toggle"));
    expect(screen.getByTestId("live-thinking-text")).toBeVisible();

    // 逐字追加：新增 delta 后同一块继续增长。
    rerender(<CompactHarnessTimeline {...props} isStreaming messages={[
      live[0],
      { ...live[1], blocks: [{ id: "t1", type: "thinking" as const, source: "provider_reasoning" as const, content: "第一步：读取工作区，然后分析", status: "streaming" as const }] },
    ]} />);
    expect(screen.getByTestId("live-thinking-text")).toHaveTextContent("第一步：读取工作区，然后分析");

    // 思考结束：脉冲停止，且不渲染"累计多少字"这类计数。
    rerender(<CompactHarnessTimeline {...props} isStreaming messages={[
      live[0],
      { ...live[1], content: "答案", blocks: [{ id: "t1", type: "thinking" as const, source: "provider_reasoning" as const, content: "第一步：读取工作区，然后分析", status: "done" as const }] },
    ]} />);
    const finished = screen.getByTestId("live-thinking");
    expect(finished).toHaveAttribute("data-streaming", "false");
    expect(finished).not.toHaveTextContent("正在思考…");
    expect(finished.textContent).not.toMatch(/\d+\s*字/);
  });
  it("does not attribute aggregate turn usage to an individual thinking segment", () => {
    render(<CompactHarnessTimeline {...props} isStreaming reasoningTokens={2048} messages={[
      { id: "user", role: "user", timestamp: 1, content: "问题" },
      { id: "live", role: "model", timestamp: 2, content: "", invocationId: "run-live",
        blocks: [{ id: "t1", type: "thinking", source: "public_summary", content: "推理中", status: "streaming" }] },
    ]} />);
    expect(screen.queryByTestId("live-thinking-tokens")).toBeNull();
    expect(screen.getByTestId("live-thinking").textContent).not.toMatch(/\d+\s*字/);
  });
  it("keeps the historical and compacted transcript on the summary-only policy", () => {
    const { container } = render(<CompactHarnessTimeline {...props} messages={[
      { id: "user", role: "user", timestamp: 1, content: "调研" },
      { id: "history", role: "model", timestamp: 2, content: "结果", invocationId: "run-old",
        reasoning: "内部参数 token=secret，具体推理过程",
        blocks: [{ id: "t1", type: "thinking", content: "内部参数 token=secret，具体推理过程", status: "done" }] },
    ]} />);
    expect(screen.queryByTestId("live-thinking")).toBeNull();
    expect(container.textContent).not.toContain("secret");
    expect(container.textContent).not.toContain("具体推理过程");
  });
  it("keeps the last live thinking snapshot visible after the run settles", () => {
    const live = [
      { id: "user", role: "user" as const, timestamp: 1, content: "问题" },
      { id: "live", role: "model" as const, timestamp: 2, content: "", invocationId: "run-live",
        blocks: [{ id: "t1", type: "thinking" as const, source: "public_summary" as const, content: "推理快照", status: "streaming" as const }] },
    ];
    const { rerender } = render(<CompactHarnessTimeline {...props} isStreaming messages={live} />);
    expect(screen.getByTestId("live-thinking")).toHaveTextContent("推理快照");
    rerender(<CompactHarnessTimeline {...props} isStreaming={false} reasoningTokens={77} messages={live} />);
    const pinned = screen.getByTestId("live-thinking");
    expect(pinned).toHaveAttribute("data-streaming", "false");
    const details = pinned.querySelector("details")!;
    details.open = true; fireEvent(details, new Event("toggle"));
    expect(pinned).toHaveTextContent("推理快照");
    expect(screen.queryByTestId("live-thinking-tokens")).toBeNull();
    for (const message of screen.getAllByTestId("shared-message")) {
      expect(message).toHaveAttribute("data-agent-header", "false");
    }
  });
});

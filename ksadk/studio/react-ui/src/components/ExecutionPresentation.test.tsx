import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ToolExecutionCard, TaskPlan, diffLines, type ToolExecution } from "./ToolExecutionCard";
import { LiveThinking } from "./LiveThinking";
import { HarnessActivity } from "./HarnessActivity";
import { apiFetch } from "../api";

vi.mock("../api", () => ({ apiFetch: vi.fn() }));
afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals(); vi.resetAllMocks(); });
const tool: ToolExecution = { id: "tool", callId: "call", status: "running",
  display: { version: "tool-display/v1", category: "terminal", summary: "运行命令：pytest", output: "first" } };

describe("public execution presentation", () => {
  it("names a historical builtin without inventing missing files or line counts", () => {
    const { container, rerender } = render(<ToolExecutionCard tool={{ ...tool,
      name: "browser_inspect_workspace", status: "completed",
      display: { version: "tool-display/v1", category: "tool" },
    }} />);
    expect(screen.getByText("浏览器检查")).toBeVisible();
    expect(container.querySelector(".harness-change-counts")).toBeNull();
    expect(container.querySelector(".harness-file-name")).toBeNull();
    rerender(<ToolExecutionCard tool={{ ...tool, name: "custom_private_tool",
      display: { version: "tool-display/v1", category: "tool" },
    }} />);
    expect(screen.getByText("工具调用")).toBeVisible();
    expect(container).not.toHaveTextContent("custom_private_tool");
  });
  it("distinguishes a completed launcher from its running background process", () => {
    const { container, rerender } = render(<ToolExecutionCard tool={{ ...tool, name: "run_command", status: "completed",
      display: { ...tool.display, background_status: "running" } }} />);
    expect(screen.getByText("后台运行")).toBeVisible();
    expect(container.querySelector("details")).toHaveAttribute("data-status", "running");
    expect(screen.queryByText("已完成")).toBeNull();
    rerender(<ToolExecutionCard tool={{ ...tool, name: "run_command", status: "completed",
      display: { ...tool.display, background_status: "completed", exit_code: 0 } }} />);
    expect(screen.getByText("后台已完成")).toBeVisible();
    expect(container.querySelector("details")).toHaveAttribute("data-status", "completed");
    expect(screen.queryByText("后台运行")).toBeNull();
  });
  it("shows a pending cancellation neutrally until the owned process has stopped", () => {
    const { container, rerender } = render(<ToolExecutionCard tool={{ ...tool, name: "stop_command", status: "completed",
      display: { ...tool.display, background_status: "running", background_cancel_requested: true } }} />);
    expect(screen.getByText("停止后台命令")).toBeVisible();
    expect(screen.getByText("正在取消")).toBeVisible();
    expect(screen.queryByText("已取消")).toBeNull();
    rerender(<ToolExecutionCard tool={{ ...tool, name: "stop_command", status: "completed",
      display: { ...tool.display, background_status: "cancelled", exit_code: -15 } }} />);
    expect(screen.getByText("已取消")).toBeVisible();
    expect(container.querySelector("details")).toHaveAttribute("data-status", "cancelled");
  });
  it.each(["failed", "timed_out", "output_limit"] as const)("shows a process failure even when its query RPC succeeds (%s)", background_status => {
    const { container } = render(<ToolExecutionCard tool={{ ...tool, name: "read_command_output", status: "completed",
      display: { ...tool.display, background_status, exit_code: 7 } }} />);
    expect(screen.getByText("读取后台输出")).toBeVisible();
    expect(screen.getByText("退出码 7")).toBeVisible();
    expect(container.querySelector("details")).toHaveAttribute("data-status", "failed");
  });
  it("keeps an unknown background outcome separate from a confirmed completion", () => {
    const { container } = render(<ToolExecutionCard tool={{ ...tool, name: "read_command_output", status: "completed",
      display: { ...tool.display, background_status: "unknown" } }} />);
    expect(screen.getByText("后台结果未确认")).toBeVisible();
    expect(container.querySelector("details")).toHaveAttribute("data-status", "unknown");
    expect(screen.queryByText("后台已完成")).toBeNull();
  });
  it("keeps a resumed turn out of the legacy thinking counter and duplicate commentary", async () => {
    vi.mocked(apiFetch).mockResolvedValue(new Response(JSON.stringify({ runId: "run-original", status: "running", activities: [
      { id: "reason", kind: "step", label: "思考", category: "reasoning", status: "running", count: 7, durationMs: 46000, details: [] },
      { id: "comment", kind: "commentary", label: "公开进展", status: "completed", text: "正在修复当前游戏", details: [] },
    ] })));
    const { container, rerender } = render(<HarnessActivity runId="run-resume" streaming fallback={[]}
      canonicalTimeline showExecutionDetails={false} />);
    expect(await screen.findByText("正在思考")).toBeVisible();
    expect(screen.queryByText("· 46 秒")).not.toBeInTheDocument();
    expect(container.querySelector("details")).toBeNull();
    expect(container).not.toHaveTextContent("7 次");
    expect(container).not.toHaveTextContent("正在运行");
    expect(container).not.toHaveTextContent("正在修复当前游戏");
    rerender(<HarnessActivity runId="run-resume" streaming fallback={[]}
      canonicalTimeline visibleThinking showExecutionDetails={false} />);
    expect(screen.queryByTestId("live-thinking")).toBeNull();
  });
  it("shows the host failure reason while the tool remains collapsed", () => {
    const { container } = render(<ToolExecutionCard tool={{ ...tool, status: "failed", display: {
      version: "tool-display/v1", category: "read", summary: "查看文件：capture.png", path: "capture.png",
      error_message: "这是图片文件，文本读取工具无法查看；需要图像工具或视觉模型。",
    } }} />);
    expect(screen.getByText(/这是图片文件/)).toBeVisible();
    expect(container.querySelector("details")!.open).toBe(false);
    expect(screen.getByText("失败")).toBeVisible();
    expect(screen.queryByLabelText("工具输出")).toBeNull();
  });
  it("keeps file actions expandable with a trailing disclosure and a separate file link", async () => {
    const { container } = render(<ToolExecutionCard runId="run-original" tool={{ ...tool, name: "write_workspace_file", status: "completed", display: {
      version: "tool-display/v1", category: "edit", summary: "保存文件：player.js", path: "player.js",
      workspace_path: "starark/js/player.js", additions: 107, diff: "+export const player = {};",
    } }} />);
    const details = container.querySelector("details")!;
    const summary = details.querySelector("summary")!;
    expect(screen.getByText("写入")).toBeVisible();
    expect(summary.firstElementChild).not.toHaveClass("activity-chevron");
    expect(summary.lastElementChild).toHaveClass("harness-tool-disclosure");
    fireEvent.click(screen.getByRole("link", { name: "player.js" }));
    expect(details.open).toBe(false);
    fireEvent.click(summary);
    expect(await screen.findByLabelText("编辑 diff")).toHaveTextContent("+export const player = {};");
    expect(details.open).toBe(true);
  });
  it("accepts omitted empty summaries from the public API without crashing the timeline", () => {
    render(<ToolExecutionCard tool={{ ...tool, status: "completed", display: {
      version: "tool-display/v1", category: "terminal", output: "recorded", exit_code: 0,
    } }} />);
    expect(screen.getByText("终端")).toBeVisible();
    expect(screen.queryByText("已完成")).toBeNull();
    expect(screen.queryByText("退出码 0")).toBeNull();
  });
  it("opens the recorded workspace path through the owning run and avoids duplicated file labels", () => {
    render(<ToolExecutionCard runId="run-original" tool={{ ...tool, status: "completed", display: {
      version: "tool-display/v1", category: "edit", summary: "编辑文件：app.py", path: "app.py",
      workspace_path: "src/app.py",
    } }} />);
    expect(screen.getByRole("link", { name: "app.py" })).toHaveAttribute("href",
      "/api/v1/runs/run-original/documents/content?path=src%2Fapp.py");
    expect(screen.queryByText("编辑文件：app.py")).toBeNull();
    const details = screen.getByRole("link", { name: "app.py" }).closest("details")!;
    fireEvent.click(screen.getByRole("link", { name: "app.py" }));
    expect(details.open).toBe(false);
  });
  it("shows the file type and host-applied counts without deriving them from a clipped diff", () => {
    render(<ToolExecutionCard tool={{ ...tool, status: "completed", display: {
      version: "tool-display/v1", category: "edit", summary: "编辑文件", path: "app.py",
      diff: "-old\n+new\n", truncated: true, additions: 900, deletions: 700,
    } }} />);
    expect(screen.getByText("PY")).toBeVisible();
    expect(screen.getByText("app.py")).toBeVisible();
    expect(screen.getByText("+900")).toBeVisible();
    expect(screen.getByText("−700")).toBeVisible();
  });
  it("shows a terminal interruption even when every preceding tool succeeded", async () => {
    const source = vi.fn(function () { return { close: vi.fn(), addEventListener: vi.fn() }; }); vi.stubGlobal("EventSource", source);
    vi.mocked(apiFetch).mockResolvedValue(new Response(JSON.stringify({
      runId: "run", status: "failed", activities: [], terminal: {
        status: "interrupted", reason: "effect_outcome_unknown", outcome: "unverified",
      },
    })));
    render(<HarnessActivity runId="run" streaming fallback={[]} />);
    expect(await screen.findByRole("alert")).toHaveTextContent("任务已中断");
    expect(screen.getByRole("alert")).toHaveTextContent("结果未确认");
    expect(source).not.toHaveBeenCalled();
  });
  it("keeps a user-cancelled turn neutral without hiding an uncertain tool receipt", async () => {
    vi.mocked(apiFetch).mockResolvedValue(new Response(JSON.stringify({
      runId: "run-cancelled", status: "cancelled", terminal: { status: "cancelled" },
      activities: [{ id: "command", kind: "step", label: "运行命令", status: "unknown", details: [
        { id: "call", text: "未确认完成：运行命令" },
      ] }],
    })));
    const { container } = render(<HarnessActivity runId="run-cancelled" streaming={false} fallback={[]} />);
    await screen.findByText("执行记录");
    expect(screen.queryByRole("alert")).toBeNull();
    expect(container.querySelector(".harness-run-stopped")).toBeNull();
    expect(container.querySelector(".harness-execution-details")).not.toHaveAttribute("data-status", "attention");
    fireEvent.click(screen.getByText("执行记录"));
    fireEvent.click(screen.getByText("运行命令"));
    expect(screen.getByText("未确认完成：运行命令")).toBeVisible();
  });
  it("retains genuine approval warnings on a cancelled turn", async () => {
    vi.mocked(apiFetch).mockResolvedValue(new Response(JSON.stringify({
      runId: "run-cancelled", status: "cancelled", terminal: { status: "cancelled" },
      activities: [], approvalAlerts: [{ id: "approval", status: "unknown" }],
    })));
    render(<HarnessActivity runId="run-cancelled" streaming={false} fallback={[]} />);
    expect(await screen.findByRole("alert")).toHaveTextContent("审批结果未确认");
    expect(screen.queryByText("任务已取消")).toBeNull();
  });
  it("keeps the full authorized summary when expanded and follows the horizontal tail", async () => {
    const text = "original observation " + "x".repeat(1100) + "\nlatest line";
    const { rerender } = render(<LiveThinking text={text} streaming />);
    const preview = screen.getByTestId("thinking-line-scroll");
    Object.defineProperty(preview, "scrollWidth", { configurable: true, value: 1300 });
    rerender(<LiveThinking text={text + " continues"} streaming />);
    expect(preview.scrollLeft).toBe(1300);
    const details = screen.getByRole("status").closest("details")!;
    details.open = true; fireEvent(details, new Event("toggle"));
    expect(await screen.findByTestId("live-thinking-text")).toHaveTextContent("original observation");
    expect(screen.getByTestId("live-thinking-text").textContent).toBe(text + " continues");
    expect(screen.queryByText(/仅显示最近一段/)).toBeNull();
  });
  it("keeps old and new line positions across diff hunks", () => {
    const rows = diffLines("@@ -3,2 +10,2 @@\n-old\n+new\n same\n@@ -30 +40 @@\n-last\n+next");
    expect(rows[1]).toMatchObject({ before: 3, after: undefined, change: "remove" });
    expect(rows[2]).toMatchObject({ before: undefined, after: 10, change: "add" });
    expect(rows[3]).toMatchObject({ before: 4, after: 11 });
    expect(rows[5].before).toBe(30); expect(rows[6].after).toBe(40);
  });
  it("mounts output on disclosure, freezes when scrolled up and resumes explicitly", async () => {
    const { rerender } = render(<ToolExecutionCard tool={tool} />);
    expect(screen.queryByLabelText("工具输出")).toBeNull();
    const details = screen.getByText("pytest").closest("details")!;
    details.open = true; fireEvent(details, new Event("toggle"));
    const output = await screen.findByLabelText("工具输出");
    Object.defineProperties(output, { scrollHeight: { configurable: true, value: 1000 }, clientHeight: { configurable: true, value: 100 } });
    output.scrollTop = 100; fireEvent.scroll(output);
    rerender(<ToolExecutionCard tool={{ ...tool, display: { ...tool.display, output: "first\nsecond" } }} />);
    expect(output.scrollTop).toBe(100);
    fireEvent.click(screen.getByText("跟随最新输出"));
    expect(output.scrollTop).toBe(1000);
    rerender(<ToolExecutionCard tool={{ ...tool, status: "failed", display: { ...tool.display, truncated: true, total_chars: 30000, exit_code: 1 } }} />);
    expect(screen.getByText("退出码 1")).toBeVisible();
    expect(screen.getByText(/输出已截断/)).toBeVisible();
    expect(screen.getByText("失败")).toBeVisible();
  });

  it("renders a text-only applied diff and a separate model plan", async () => {
    render(<><ToolExecutionCard tool={{ ...tool, display: { ...tool.display, category: "edit", diff: "-old\n+<script>fixture</script>" } }} />
      <TaskPlan items={[{ id: "stable", content: "Run checks", status: "in_progress", priority: "high" }]} /></>);
    const details = screen.getByText("运行命令：pytest").closest("details")!;
    details.open = true; fireEvent(details, new Event("toggle"));
    const diff = await screen.findByLabelText("编辑 diff");
    expect(diff.querySelector("script")).toBeNull();
    expect(diff.querySelector('[data-change="add"]')).toHaveTextContent("+<script>fixture</script>");
    expect(screen.getByText("0/1")).toBeVisible();
    expect(screen.queryByText(/验收和验证结果以审计记录为准/)).toBeNull();
    const plan = screen.getByText("0/1").closest("details")!;
    expect(plan.open).toBe(false);
  });

  it("keeps manual thinking expansion through completion and freezes observed time", () => {
    vi.useFakeTimers();
    const { rerender } = render(<LiveThinking text={"first\nlatest"} streaming />);
    expect(screen.getByText("latest")).toBeVisible();
    const details = screen.getByText("正在思考").closest("details")!;
    details.open = true; fireEvent(details, new Event("toggle"));
    act(() => vi.advanceTimersByTime(3000));
    expect(screen.getByText("· 3 秒")).toBeVisible();
    rerender(<LiveThinking text={"first\nlatest"} streaming={false} />);
    expect(details.open).toBe(true);
    act(() => vi.advanceTimersByTime(5000));
    expect(screen.getByText("· 持续了 3 秒")).toBeVisible();
  });

  it("updates activity by SSE and releases the observer on terminal state", async () => {
    const listeners = new Map<string, (event: MessageEvent) => void>();
    const close = vi.fn();
    const source = vi.fn(function () { return { close, addEventListener: (name: string, callback: (event: MessageEvent) => void) => listeners.set(name, callback) }; });
    vi.stubGlobal("EventSource", source);
    vi.mocked(apiFetch).mockResolvedValue(new Response(JSON.stringify({ runId: "run", status: "running", activities: [] })));
    const { unmount } = render(<HarnessActivity runId="run" streaming fallback={[]} />);
    await waitFor(() => expect(source).toHaveBeenCalledOnce());
    act(() => listeners.get("snapshot")!(new MessageEvent("snapshot", { data: JSON.stringify({ runId: "run", status: "completed", activities: [], approvalAlerts: [{ id: "approval", status: "unknown" }] }) })));
    expect(screen.getByRole("alert")).toHaveTextContent("审批结果未确认");
    expect(close).toHaveBeenCalled();
    unmount();
  });
});


it("downloads only an opaque artifact through the current run scope", async () => {
  const identity = "a".repeat(64);
  const { rerender } = render(<ToolExecutionCard runId="run/original" tool={{ ...tool,
    display: { ...tool.display, artifact_id: identity, truncated: true } }} />);
  const details = screen.getByText("pytest").closest("details")!;
  details.open = true; fireEvent(details, new Event("toggle"));
  expect(await screen.findByRole("link", { name: "下载已保留输出" })).toHaveAttribute("href",
    `/api/v1/runs/run%2Foriginal/public-output/${identity}`);
  rerender(<ToolExecutionCard runId="run/original" tool={{ ...tool,
    display: { ...tool.display, artifact_id: "https://example.invalid/untrusted" } }} />);
  expect(screen.queryByRole("link")).toBeNull();
});

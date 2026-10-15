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
    const details = screen.getByText("运行命令：pytest").closest("details")!;
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
    expect(screen.getByText(/验收和验证结果以审计记录为准/)).toBeVisible();
  });

  it("keeps manual thinking expansion through completion and freezes observed time", () => {
    vi.useFakeTimers();
    const { rerender } = render(<LiveThinking text={"first\nlatest"} streaming />);
    expect(screen.getByText("latest")).toBeVisible();
    const details = screen.getByText("正在思考…").closest("details")!;
    details.open = true; fireEvent(details, new Event("toggle"));
    act(() => vi.advanceTimersByTime(3000));
    expect(screen.getByText("观测 3s")).toBeVisible();
    rerender(<LiveThinking text={"first\nlatest"} streaming={false} />);
    expect(details.open).toBe(true);
    act(() => vi.advanceTimersByTime(5000));
    expect(screen.getByText("观测 3s")).toBeVisible();
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
  const details = screen.getByText("运行命令：pytest").closest("details")!;
  details.open = true; fireEvent(details, new Event("toggle"));
  expect(await screen.findByRole("link", { name: "下载已保留输出" })).toHaveAttribute("href",
    `/api/v1/runs/run%2Foriginal/public-output/${identity}`);
  rerender(<ToolExecutionCard runId="run/original" tool={{ ...tool,
    display: { ...tool.display, artifact_id: "https://example.invalid/untrusted" } }} />);
  expect(screen.queryByRole("link")).toBeNull();
});

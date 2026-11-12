import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { apiFetch } from "../api";
import { CompactHarnessTimeline } from "./CompactHarnessTimeline";

// Exercise the locked shared renderer, not a mocked ChatMessageList. Only the
// public Studio HTTP boundary is substituted; no model or game is started.
vi.mock("../api", () => ({ apiFetch: vi.fn() }));
afterEach(() => vi.resetAllMocks());

it("renders public reasoning and tools inline, with the recorded plan only in the side dock", async () => {
  vi.mocked(apiFetch).mockImplementation(async path => new Response(JSON.stringify(
    String(path).includes("/activities") ? {
      runId: "run-unified", status: "cancelled", activities: [],
    } : { items: [{ id: "run-unified", status: "CANCELLED", durationMs: 62000 }] },
  )));
  const { container } = render(<CompactHarnessTimeline agentName="Harness" isMobile={false}
    isStreaming={false} activity={null} sessionId="session-unified"
    onDeleteFeedback={vi.fn()} onSubmitFeedback={vi.fn()} onRespondToApproval={vi.fn()}
    messages={[
      { id: "user", role: "user", timestamp: 1, content: "Inspect the current project" },
      { id: "assistant", role: "model", timestamp: 2, content: "Inspection stopped.", runId: "run-unified", blocks: [
        { id: "reason", type: "thinking", source: "public_summary", status: "done", content: "Inspecting the project structure" },
        { id: "plan", type: "tool", toolName: "TodoWrite", status: "completed", args: "", extra: { toolDisplay: {
          version: "tool-display/v1", category: "todo", todos: [
            { id: "inspect", content: "Inspect project files", status: "completed", priority: "high" },
            { id: "verify", content: "Verify behavior", status: "pending", priority: "high" },
          ],
        } } },
        { id: "read", type: "tool", toolName: "read_workspace_file", status: "completed", args: "", extra: { toolDisplay: {
          version: "tool-display/v1", category: "read", label: "查看文件", files: [{ path: "sample.ts" }],
        } } },
        { id: "answer", type: "text", status: "done", content: "Inspection stopped." },
      ] },
    ]} />);
  const transcript = container.querySelector(".harness-timeline")!;
  const dock = container.querySelector(".harness-plan-dock")!;
  expect(dock).toHaveTextContent("进程1/2");
  expect(transcript.contains(dock)).toBe(false);
  expect(transcript).not.toHaveTextContent("执行记录");
  expect(transcript.querySelector(".harness-task-plan")).toBeNull();
  fireEvent.click(screen.getByText("思考", { exact: true }));
  await waitFor(() => expect(transcript).toHaveTextContent("Inspecting the project structure"));
  expect(transcript).toHaveTextContent("读取文件");
  expect(transcript).toHaveTextContent("Inspection stopped.");
  const details = dock.querySelector("details")!;
  expect(details.open).toBe(false);
  fireEvent.click(dock.querySelector("summary")!);
  await waitFor(() => expect(details.open).toBe(true));
  expect(dock).toHaveTextContent("Inspect project files");
  await waitFor(() => expect(screen.getByTestId("run-elapsed")).toHaveTextContent("已取消 1 分 2 秒"));
});

import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { CompactHarnessTimeline } from "./CompactHarnessTimeline";

vi.mock("@kingsoftcloud/ksadk-web/chat/timeline", () => ({ ChatMessageList: ({ messages }: { messages: unknown[] }) => <div data-testid="shared-message">{JSON.stringify(messages)}</div>, StatusBanner: () => <div data-testid="status-banner" /> }));
vi.mock("./HarnessActivity", () => ({ HarnessActivity: ({ runId }: { runId: string }) => <div data-testid="activity">{runId}</div> }));
const props = { agentName: "Harness", isMobile: false, isStreaming: false, activity: null, sessionId: "ses-1",
  onDeleteFeedback: vi.fn(), onSubmitFeedback: vi.fn(), onRespondToApproval: vi.fn() };

describe("Harness timeline integration", () => {
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

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { CompactHarnessTimeline } from "./CompactHarnessTimeline";

// Keep the actual package ChatMessageList: mocking the shared
// renderer previously concealed its legacy fallback when metadata was lost.
vi.mock("./HarnessActivity", async original => ({ ...await original<typeof import("./HarnessActivity")>(), HarnessActivity: () => null }));
vi.mock("./RunElapsed", () => ({ RunElapsed: () => null }));
const props = { agentName: "Harness", isMobile: false, isStreaming: false, activity: null,
  sessionId: "session", onDeleteFeedback: vi.fn(), onSubmitFeedback: vi.fn(), onRespondToApproval: vi.fn() };

describe("real shared renderer execution surface", () => {
  it("renders one provider segment when live and restored messages overlap", () => {
    const thinking = { type: "thinking" as const, source: "provider_reasoning" as const,
      status: "streaming" as const, content: "Checking saved world data" };
    const { container } = render(<CompactHarnessTimeline {...props} isStreaming activeRunId="run-public" messages={[
      { id: "conversation:run-public:thought", itemId: "thought", role: "model", content: "", timestamp: 1,
        runId: "run-public", eventType: "conversation_item_v1",
        blocks: [{ ...thinking, id: "conversation-block:thought" }] },
      { id: "run-public:live", role: "model", content: "", timestamp: 2, invocationId: "run-public",
        blocks: [{ ...thinking, id: "thought", content: "Checking saved world data and capacity" }] },
    ]} />);
    expect(screen.getAllByTestId("live-thinking")).toHaveLength(1);
    expect(screen.getByTestId("live-thinking")).toHaveTextContent("Checking saved world data and capacity");
    expect(container.querySelector(".harness-live-run")).toBeNull();
  });

  it("groups canonical read receipts and preserves their file links", async () => {
    // Same view shape produced by projectHostedConversation from the server.
    const messages = ["app.py", "test.py"].map((path, index) => ({
      id: `run-public:${index}`, role: "model" as const, content: "", timestamp: index,
      runId: "run-public", blocks: [{ id: `call-${index}`, type: "tool" as const,
        toolName: "read_workspace_file", status: "completed" as const, args: "",
        extra: { callId: `call-${index}`, toolDisplay: { version: "tool-display/v1", category: "read",
          summary: `读取文件：${path}`, path, workspace_path: `src/${path}` } } }],
    }));
    const { container } = render(<CompactHarnessTimeline {...props} messages={messages} />);
    expect(container.querySelectorAll(".harness-tool-group")).toHaveLength(1);
    expect(screen.getByText("2 文件")).toBeVisible();
    const group = container.querySelector<HTMLDetailsElement>(".harness-tool-group")!;
    group.open = true; fireEvent(group, new Event("toggle"));
    await waitFor(() => expect(screen.getByRole("link", { name: "app.py" })).toBeVisible());
    expect(container.querySelectorAll(".harness-tool-card")).toHaveLength(2);
    expect(screen.queryByText(/已完成.*读取文件/)).toBeNull();
  });

  it("keeps old calls without receipts on the same surface without exposing raw output", () => {
    const { container } = render(<CompactHarnessTimeline {...props} messages={[{
      id: "run-public:old", role: "model", content: "", timestamp: 1, runId: "run-public",
      blocks: [{ id: "old", type: "tool", toolName: "read_workspace_file", status: "completed",
        args: '{"password":"private"}', output: "private-result" }],
    }]} />);
    expect(container.querySelectorAll(".harness-tool-card")).toHaveLength(1);
    // A known builtin can recover its fixed action name without recovering
    // private arguments or inventing the absent historical path/counts.
    expect(screen.getByText("读取文件")).toBeVisible();
    expect(container).not.toHaveTextContent("private-result");
    expect(container).not.toHaveTextContent("password");
  });
});

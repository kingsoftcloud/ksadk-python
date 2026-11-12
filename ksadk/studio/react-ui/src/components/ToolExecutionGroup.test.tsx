import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { ReactNode } from "react";
import { CompactHarnessTimeline } from "./CompactHarnessTimeline";
import { buildToolWorkItems, type WorkBlock } from "./toolWorkItems";

// Exercise the host callback at each canonical block position. A null result
// consumes a grouped child; undefined retains the shared approval renderer.
vi.mock("@kingsoftcloud/ksadk-web/chat/timeline", () => ({
  ChatMessageList: ({ messages, renderProcessingBlock }: {
    messages: { blocks?: WorkBlock[]; content: string }[];
    renderProcessingBlock: (block: WorkBlock) => ReactNode | undefined;
  }) => <>{messages.map((message, index) => <div key={index}>{message.blocks?.map(block => {
    const rendered = renderProcessingBlock(block);
    return <div key={block.id}>{rendered !== undefined ? rendered
      : <p data-testid="shared-fallback">{block.type === "text" ? "公开进展" : `审批 ${block.id}`}</p>}</div>;
  })}</div>)}</>, StatusBanner: () => null,
}));
vi.mock("./HarnessActivity", () => ({ HarnessActivity: () => null }));
const props = { agentName: "Harness", isMobile: false, isStreaming: false, activity: null, sessionId: "session",
  onDeleteFeedback: vi.fn(), onSubmitFeedback: vi.fn(), onRespondToApproval: vi.fn() };
const read = (id: string, status = "completed") => ({ id, type: "tool" as const, toolName: "read_workspace_file",
  status: status as "completed" | "running" | "error", args: "", extra: { callId: `call-${id}`,
    toolDisplay: { version: "tool-display/v1", category: "read", summary: `读取文件：${id}.py`, path: `${id}.py`, workspace_path: `src/${id}.py` } } });
const message = (blocks: (ReturnType<typeof read> & { extra: Record<string, unknown> })[]) => [{ id: "run-a:work", role: "model" as const,
  invocationId: "run-a", timestamp: 1, content: "", blocks }];

describe("contiguous execution groups", () => {
  it("shows the first tool immediately and groups only after the second arrives", async () => {
    const { container, rerender } = render(<CompactHarnessTimeline {...props} messages={message([read("a")])} />);
    expect(screen.getByRole("link", { name: "a.py" })).toBeVisible();
    expect(container.querySelector(".harness-tool-group")).toBeNull();
    rerender(<CompactHarnessTimeline {...props} messages={message([read("a"), read("b")])} />);
    expect(screen.getByText("2 文件")).toBeVisible();
    expect(container.querySelectorAll(".harness-tool-group")).toHaveLength(1);
    expect(screen.queryByRole("link", { name: "a.py" })).toBeNull();
    const group = screen.getByText("查阅").closest("details")!;
    group.open = true; fireEvent(group, new Event("toggle"));
    await waitFor(() => expect(container.querySelectorAll("[data-call-id]")).toHaveLength(2));
    expect([...container.querySelectorAll<HTMLElement>("[data-call-id]")].map(row => row.dataset.callId))
      .toEqual(["call-a", "call-b"]);
    expect(screen.getByRole("link", { name: "b.py" })).toHaveAttribute("href",
      "/api/v1/runs/run-a/documents/content?path=src%2Fb.py");
  });

  it("keeps the same open group and call identity while a stream grows", async () => {
    const { container, rerender } = render(<CompactHarnessTimeline {...props} isStreaming messages={message([read("a"), read("b")])} />);
    const group = container.querySelector<HTMLDetailsElement>(".harness-tool-group")!;
    expect(group).toHaveAttribute("data-running", "true");
    group.open = true; fireEvent(group, new Event("toggle"));
    await screen.findByRole("link", { name: "b.py" });
    rerender(<CompactHarnessTimeline {...props} isStreaming messages={message([read("a"), read("b"), read("c", "running")])} />);
    expect(container.querySelector(".harness-tool-group")).toBe(group);
    expect(group.open).toBe(true);
    expect(screen.getByText("3 文件")).toBeVisible();
    expect(container.querySelectorAll('[data-call-id="call-c"]')).toHaveLength(1);
    rerender(<CompactHarnessTimeline {...props} messages={message([read("a"), read("b"), read("c")])} />);
    expect(group).toHaveAttribute("data-running", "false");
    expect(group.open).toBe(true);
  });

  it("retains text, authorized thinking, approvals and writes as group boundaries", () => {
    const blocks: WorkBlock[] = [read("a"), read("b"), { id: "text", type: "text", status: "done" },
      read("c"), { ...read("approval"), extra: { ...read("approval").extra, approvalRequestId: "approval-id" } },
      read("d"), { id: "thinking", type: "thinking", status: "streaming" }, read("e"),
      { ...read("write"), extra: { ...read("write").extra, toolDisplay: { ...read("write").extra.toolDisplay, category: "edit" } } }, read("f")];
    const work = buildToolWorkItems(blocks, true);
    expect([...work.groupedChildren]).toEqual(["b"]);
    expect(work.first.get("a")?.running).toBe(false);
    expect(work.first.has("approval")).toBe(false);
    expect(work.first.get("write")?.family).toBeNull();
    expect(work.first.get("f")?.running).toBe(true);
    expect(blocks.map(block => block.id)).toEqual(["a", "b", "text", "c", "approval", "d", "thinking", "e", "write", "f"]);
  });

  it("automatically reveals failures inside an existing group without losing earlier calls", async () => {
    const { container, rerender } = render(<CompactHarnessTimeline {...props} messages={message([read("a"), read("b")])} />);
    const group = container.querySelector<HTMLDetailsElement>(".harness-tool-group")!;
    expect(group.open).toBe(false);
    rerender(<CompactHarnessTimeline {...props} messages={message([read("a"), read("b", "error")])} />);
    expect(await screen.findByText("1 项失败")).toBeVisible();
    await waitFor(() => expect(group.open).toBe(true));
    expect(container.querySelectorAll("[data-call-id]")).toHaveLength(2);
    expect(container.querySelector('[data-call-id="call-b"]')).toHaveAttribute("data-status", "failed");
  });

  it("keeps approval handling in the shared renderer and never groups across it", () => {
    render(<CompactHarnessTimeline {...props} messages={message([read("a"),
      { ...read("approval"), extra: { ...read("approval").extra, approvalRequestId: "approval-id" } }, read("b")])} />);
    expect(screen.getByTestId("shared-fallback")).toHaveTextContent("审批 approval");
    expect(screen.getByRole("link", { name: "a.py" })).toBeVisible();
    expect(screen.getByRole("link", { name: "b.py" })).toBeVisible();
    expect(screen.queryByText("查阅")).toBeNull();
  });

  it("respects a manual collapse when another failure joins the group", async () => {
    const { container, rerender } = render(<CompactHarnessTimeline {...props}
      messages={message([read("a"), read("b", "error")])} />);
    const group = container.querySelector<HTMLDetailsElement>(".harness-tool-group")!;
    await waitFor(() => expect(group.open).toBe(true));
    group.open = false; fireEvent(group, new Event("toggle"));
    await waitFor(() => expect(group.open).toBe(false));
    rerender(<CompactHarnessTimeline {...props}
      messages={message([read("a", "error"), read("b", "error")])} />);
    expect(screen.getByText("2 项失败")).toBeVisible();
    expect(group.open).toBe(false);
  });
});

import { describe, expect, it } from "vitest";
import { compactHarnessMessages } from "./compactHarnessMessages";

describe("compact Harness presentation", () => {
  it.each(["public_summary", "provider_reasoning"] as const)(
    "reconciles live and replay %s by segment identity without removing equal text from different segments",
    (source) => {
      const [result] = compactHarnessMessages([
        { id: "replay", role: "model", content: "", timestamp: 0, runId: "run",
          blocks: [{ id: "conversation-block:item-a", type: "thinking", status: "streaming", source, content: "first" }] },
        { id: "live", role: "model", content: "", timestamp: 1, runId: "run",
          blocks: [
            { id: "item-a", type: "thinking", status: "streaming", source, content: "updated" },
            { id: "item-b", type: "thinking", status: "streaming", source, content: "updated" },
          ] },
      ]);
      expect(result.blocks).toMatchObject([
        { id: "conversation-block:item-a", content: "updated" },
        { id: "item-b", content: "updated" },
      ]);
      expect(result.blocks).toHaveLength(2);
    },
  );

  it("does not reopen a finished segment when a stale live snapshot arrives", () => {
    const [result] = compactHarnessMessages([{ id: "m", role: "model", content: "", timestamp: 0,
      blocks: [
        { id: "conversation-block:item-a", type: "thinking", status: "done", source: "provider_reasoning", content: "finished" },
        { id: "item-a", type: "thinking", status: "streaming", source: "provider_reasoning", content: "stale" },
      ] }]);
    expect(result.blocks).toMatchObject([{ status: "done", content: "finished" }]);
    expect(result.blocks).toHaveLength(1);
  });

  it("keeps public summary segments and host tool display in their original order", () => {
    const [result] = compactHarnessMessages([{ id: "m", role: "model", content: "", timestamp: 0, blocks: [
      { id: "summary-a", type: "thinking", status: "done", source: "public_summary", content: "先查接口" },
      { id: "call-a", type: "tool", toolName: "read_workspace_file", status: "completed", args: "private-args",
        extra: { callId: "call-a", toolDisplay: { version: "tool-display/v1", category: "read", summary: "读取文件", path: "app.py" } } },
      { id: "summary-b", type: "thinking", status: "streaming", source: "public_summary", content: "现在修改" },
      { id: "private", type: "thinking", status: "done", source: "reasoning", content: "private-reasoning" },
    ] }]);
    expect(result.blocks?.filter(block => !(block.type === "thinking" && block.id.endsWith(":activity"))).map(block => block.id))
      .toEqual(["summary-a", "call-a", "summary-b"]);
    expect(result.blocks?.find(block => block.id === "call-a")).toMatchObject({ extra: { callId: "call-a", toolDisplay: { path: "app.py" } } });
    expect(JSON.stringify(result)).not.toContain("private-");
  });
  it("keeps successful actions between the surrounding public text without exposing payloads", () => {
    const [result] = compactHarnessMessages([{ id: "m", role: "model", content: "", timestamp: 0,
      blocks: [
        { id: "before", type: "text", status: "done", content: "先读取文件" },
        { id: "read-1", type: "tool", toolName: "read_workspace_file", status: "completed",
          args: "private-input", output: "private-output", extra: { privateMetadata: "private-value" } },
        { id: "after", type: "text", status: "done", content: "读取后修改" },
        { id: "write-1", type: "tool", toolName: "write_workspace_file", status: "completed", args: "private-write" },
      ] }]);
    expect(result.blocks?.filter(block => block.type !== "thinking").map(block => block.id))
      .toEqual(["before", "read-1", "after", "write-1"]);
    expect(JSON.stringify(result.blocks)).not.toContain("private-");
  });
  it("keeps newer approval state from the shared tool map over a stale tool block", () => {
    const [result] = compactHarnessMessages([{ id: "m", role: "model", content: "", timestamp: 0,
      blocks: [{ id: "call-1", type: "tool", toolName: "write_workspace_file", args: "{}", status: "running" }],
      tools: { write_workspace_file: { name: "write_workspace_file", args: "report.md", status: "paused", approvalRequestId: "approval-1", approvalStatus: "pending" } } }]);
    expect(result.blocks?.[0]).toMatchObject({ type: "tool", status: "paused", extra: { approvalRequestId: "approval-1" } });
  });
  it("uses runId to prevent cross-run mixing when invocationId is absent", () => {
    expect(compactHarnessMessages([
      { id: "a", role: "model", content: "one", timestamp: 0, runId: "run-a" },
      { id: "b", role: "model", content: "two", timestamp: 1, runId: "run-b" },
    ])).toHaveLength(2);
  });
  it("merges repeated activity snapshots and keeps the final answer", () => {
    const input = [
      { id: "user", role: "user" as const, content: "调研", timestamp: 0 },
      ...Array.from({ length: 20 }, (_, i) => ({ id: `step-${i}`, role: "model" as const,
        content: "", reasoning: "已搜索资料\n已查看资料", timestamp: i + 1, invocationId: "run-1" })),
      { id: "answer", role: "model" as const, content: "[报告](./report.md)", timestamp: 22, invocationId: "run-1" },
    ];
    const result = compactHarnessMessages(input);
    expect(result).toHaveLength(2);
    expect(result[1].blocks).toHaveLength(2);
    expect(result[1].blocks?.[0]).toMatchObject({ type: "thinking", content: "- 已搜索资料\n- 已查看资料" });
    expect(result[1].blocks?.[1]).toMatchObject({ type: "text", content: "[报告](./report.md)" });
    expect("reasoning" in input[1] && input[1].reasoning).toBe("已搜索资料\n已查看资料");
  });

  it("replaces each child's status while retaining its responsibility", () => {
    const [result] = compactHarnessMessages([{ id: "m", role: "model", content: "", timestamp: 0,
      reasoning: "3 个子智能体正在运行\n• 正在运行：调研 Codex\n• 正在运行：调研 DSH\n• 已完成：调研 Codex" }]);
    const text = result.blocks?.[0];
    expect(text).toMatchObject({ content: "- 调研 Codex · 已完成\n- 调研 DSH · 正在运行" });
  });

  it("does not show detailed reasoning, tool arguments, or successful tool outputs", () => {
    const [result] = compactHarnessMessages([{ id: "m", role: "model", content: "结果", timestamp: 0,
      reasoning: "内部参数 token=secret，具体推理过程", tools: { read: { name: "read_workspace_file", args: "sensitive", output: "sensitive", status: "completed" } } }]);
    expect(JSON.stringify(result.blocks)).not.toContain("sensitive");
    expect(JSON.stringify(result.blocks)).not.toContain("secret");
    expect(JSON.stringify(result.blocks)).toContain("已查看工作区内容");
  });

  it("repairs old concatenated child snapshots without leaving a false running row", () => {
    const [result] = compactHarnessMessages([{ id: "m", role: "model", content: "完成", timestamp: 0,
      reasoning: "• 正在运行：Codex 调研\n• 正在运行：ADK 调研• 已完成：Codex 调研\n• 已完成：ADK 调研" }]);
    expect(result.blocks?.[0]).toMatchObject({ content: "- Codex 调研 · 已完成\n- ADK 调研 · 已完成" });
  });

  it("retains actionable approvals, failures, and retry visibility", () => {
    const [result] = compactHarnessMessages([{ id: "m", role: "model", content: "", timestamp: 0,
      reasoning: "模型服务繁忙，2 秒后自动重试（第 2/3 次）",
      tools: {
        approval: { name: "write_workspace_file", args: "report.md", status: "paused", approvalRequestId: "approval-1" },
        failure: { name: "web_search", args: "", status: "error", output: "网络不可达" },
      } }]);
    expect(result.blocks).toHaveLength(3);
    expect(result.blocks?.[0]).toMatchObject({ content: "- 模型服务繁忙，2 秒后自动重试（第 2/3 次）" });
    expect(result.blocks?.[1]).toMatchObject({ type: "tool", extra: { approvalRequestId: "approval-1" } });
    expect(result.blocks?.[2]).toMatchObject({ type: "tool", status: "error" });
  });

  it("does not merge different runs, user turns or interactive surfaces", () => {
    const messages = [
      { id: "a", role: "model" as const, content: "第一轮", timestamp: 0, invocationId: "run-1" },
      { id: "b", role: "model" as const, content: "第二轮", timestamp: 1, invocationId: "run-2" },
      { id: "c", role: "user" as const, content: "继续", timestamp: 2 },
      { id: "d", role: "model" as const, content: "第三轮", timestamp: 3 },
    ];
    expect(compactHarnessMessages(messages)).toHaveLength(4);
  });
});

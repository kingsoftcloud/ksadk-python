import type { ComponentProps } from "react";
import type { ChatMessageList } from "@kingsoftcloud/ksadk-web/chat/timeline";

type Message = ComponentProps<typeof ChatMessageList>["messages"][number];
type Block = NonNullable<Message["blocks"]>[number];

/** Presentation only: never mutate shared chat state or the durable transcript. */
export function compactHarnessMessages(messages: Message[]): Message[] {
  const output: Message[] = [];
  let turn: Message[] = [];
  const flush = () => {
    if (!turn.length) return;
    const activities = new Map<string, string>();
    const blocks: Block[] = [];
    let thinking = false;
    const summarize = (text: string) => {
      // Older Responses snapshots concatenated adjacent bullet lists without
      // a newline; separate their public rows before joining by responsibility.
      for (const raw of text.replace(/([^\n])•/g, "$1\n•").split(/\n+/)) {
        const line = raw.trim();
        const child = line.match(/^[•*-]\s*(正在运行|已完成|需要调整|执行失败|已取消)：(.+)$/);
        if (child) { activities.set(`child:${child[2]}`, `${child[2]} · ${child[1]}`); continue; }
        const activity = line.match(/^(正在|已)(搜索资料|查看资料|查看工作区内容|编辑并保存结果|运行任务|使用工具处理任务)(?:[。….]*)$/);
        if (activity) { activities.set(activity[2], `${activity[1]}${activity[2]}`); continue; }
        if (/^模型服务繁忙.*自动重试/.test(line)) activities.set("retry", line.slice(0, 120));
        if (/^\d+ 个子智能体/.test(line)) activities.set("children", line.slice(0, 80));
        if (/^(上下文已自动压缩|正在压缩上下文)/.test(line)) activities.set("context", line.slice(0, 80));
      }
    };
    for (const message of turn) {
      thinking ||= Boolean(message.reasoning);
      summarize(message.reasoning || "");
      const source = message.blocks?.length ? message.blocks : [
        ...(message.reasoning ? [{ id: `${message.id}:thought`, type: "thinking" as const, status: "done" as const, content: message.reasoning }] : []),
        ...Object.entries(message.tools || {}).map(([key, tool]) => ({
          id: key, type: "tool" as const, toolName: tool.name, args: tool.args,
          output: tool.output, status: tool.status, extra: tool,
        })),
        ...(message.content ? [{ id: `${message.id}:text`, type: "text" as const, status: "done" as const, content: message.content }] : []),
      ];
      for (const original of source) {
        let block = original;
        if (block.type === "tool") {
          const extra = block.extra as Record<string, unknown> | undefined;
          const callId = extra?.callId || extra?.call_id;
          const named = message.tools?.[block.toolName];
          const tool = (callId ? Object.values(message.tools || {}).find(tool => tool.callId === callId) : undefined)
            || message.tools?.[block.id] || (named && (!callId || !named.callId || named.callId === callId) ? named : undefined);
          if (tool) block = { ...block, status: tool.status, args: tool.args, output: tool.output,
            extra: { ...block.extra, ...tool } };
        }
        if (block.type === "thinking") { thinking = true; summarize(block.content); continue; }
        // Pending/rejected approvals and failures stay actionable, never compacted away.
        if (block.type === "tool" && !block.extra?.approvalRequestId && block.status !== "error" && block.status !== "paused") {
          const name = block.toolName.toLowerCase();
          const label = /search/.test(name) ? "搜索资料" : /fetch/.test(name) ? "查看资料"
            : /write|edit/.test(name) ? "编辑并保存结果" : /read|list/.test(name) ? "查看工作区内容"
            : /delegate/.test(name) ? "调度子智能体" : "使用工具处理任务";
          activities.set(label, `${block.status === "running" ? "正在" : "已"}${label}`);
          continue;
        }
        blocks.push(block);
      }
    }
    const last = turn[turn.length - 1];
    const childDetails = [...activities.entries()].filter(([key]) => key.startsWith("child:"));
    if (childDetails.length) activities.delete("children");
    const content = [...activities.values()].slice(-10).map(line => `- ${line}`).join("\n");
    const active = turn.some(message => message.status === "running");
    if (thinking || content) blocks.unshift({
      id: `${turn[0].id}:activity`, type: "thinking", status: active ? "streaming" : "done",
      content: content || (active ? "正在分析任务" : "已完成分析"),
    });
    output.push({ ...last, id: turn[0].id, blocks, reasoning: undefined, tools: undefined });
    turn = [];
  };
  for (const message of messages) {
    if (message.role !== "model" || message.a2ui || message.aguiActivity || message.aguiActivities?.length) {
      flush(); output.push(message);
    } else {
      // Separate runs must not inherit one another's progress or failure state.
      const previous = turn[turn.length - 1];
      const previousRun = previous?.invocationId || previous?.runId || previous?.responseId;
      const currentRun = message.invocationId || message.runId || message.responseId;
      if (previousRun && currentRun && previousRun !== currentRun) flush();
      turn.push(message);
    }
  }
  flush();
  return output;
}

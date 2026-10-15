import type { ConversationBinding, ConversationId, OutboxEntry, OutboxStore } from "@kingsoftcloud/ksadk-web/conversation";

export type ReceiptIdentity = { agentId: string; sessionId: string; invocationId: string };
const runStates = ["created", "running", "paused", "waiting_input", "completed", "failed", "cancelled", "interrupted", "timed_out"] as const;
type RunState = typeof runStates[number];
export type RunReceipt = {
  SchemaVersion: 1; AgentId: string; SessionId: string; InvocationId: string;
  RunId: string; Status: RunState | "queued" | "unknown"; Source: "run" | "operation" | "unavailable";
};
export interface RunReceiptReader {
  getRunReceipt(agentId: string, sessionId: string, invocationId: string, signal?: AbortSignal): Promise<RunReceipt>;
}

export function parseRunReceipt(value: unknown, identity: ReceiptIdentity): RunReceipt {
  if (!value || typeof value !== "object") throw new Error("原任务查询没有返回有效回执。");
  const receipt = value as Record<string, unknown>;
  const valid = receipt.SchemaVersion === 1 && receipt.AgentId === identity.agentId
    && receipt.SessionId === identity.sessionId && receipt.InvocationId === identity.invocationId
    && typeof receipt.RunId === "string" && (
      (receipt.Source === "run" && Boolean(receipt.RunId)
        && runStates.includes(receipt.Status as RunState))
      || (receipt.Source === "operation" && receipt.RunId === ""
        && ["queued", "running", "failed", "cancelled", "interrupted"].includes(String(receipt.Status)))
      || (receipt.Source === "unavailable" && receipt.RunId === "" && receipt.Status === "unknown")
    );
  if (!valid) throw new Error("回执与原消息身份或运行状态不匹配，保留原消息等待确认。");
  return receipt as RunReceipt;
}

/** Consume the original receipt into the existing outbox; never resend input. */
export async function reconcileOutboxEntry(options: {
  api: RunReceiptReader; outbox: OutboxStore; entry: OutboxEntry;
  agentId: string; conversationId: ConversationId; binding?: ConversationBinding;
  signal: AbortSignal; isRequestActive: (requestId: string) => boolean;
}): Promise<void> {
  const { api, outbox, entry, agentId, conversationId, binding, signal, isRequestActive } = options;
  const current = () => !signal.aborted && outbox.get(entry.requestId) === entry
    && !isRequestActive(entry.requestId);
  if (!current() || entry.agentId !== agentId || entry.conversationId !== conversationId
    || !["unknown", "sending"].includes(entry.status)) return;
  const sessionId = entry.nativeSessionId || (binding?.agentId === agentId
    && binding.conversationId === conversationId ? binding.nativeSessionId : undefined);
  if (!sessionId || !entry.invocationId) return;
  try {
    const receipt = parseRunReceipt(await api.getRunReceipt(agentId, sessionId, entry.invocationId, signal), {
      agentId, sessionId, invocationId: entry.invocationId,
    });
    if (!current()) return;
    const status = receipt.Status === "unknown" ? "unknown"
      : receipt.Status === "completed" ? "completed"
      : receipt.Status === "cancelled" ? "cancelled"
      : ["failed", "interrupted", "timed_out"].includes(receipt.Status) ? "failed" : "sending";
    const error = status === "unknown" ? "尚未查到原任务的投递证据，请查看会话记录后再确认。"
      : status === "failed" ? "原任务已失败或中断；请检查已有结果，再决定是否重新发送。" : undefined;
    if (entry.status !== status || entry.error !== error || entry.nativeSessionId !== sessionId) {
      outbox.update(entry.requestId, { status, error, nativeSessionId: sessionId });
    }
  } catch {
    if (current()) outbox.update(entry.requestId, {
      status: "unknown", error: "原任务状态查询未完成，消息不会自动重发，请稍后确认。",
    });
  }
}

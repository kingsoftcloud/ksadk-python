import type { ComponentProps } from "react";
import type { ChatMessageList } from "@kingsoftcloud/ksadk-web/chat/timeline";

type Message = ComponentProps<typeof ChatMessageList>["messages"][number];

export interface LiveThinking {
  /** Run identity the live turn belongs to (empty when the host omitted it). */
  runId: string;
  /** Reasoning text accumulated so far for the live turn, in block order. */
  text: string;
  /** True while the model is still producing this turn's reasoning. */
  streaming: boolean;
}

function runIdentity(message: Message): string {
  return String(message.invocationId || message.runId || message.responseId || "");
}

function thinkingOf(message: Message): { text: string; status: "streaming" | "done" | null } {
  const blocks = (message.blocks || []).filter(block => block.type === "thinking");
  if (blocks.length) {
    const text = blocks.map(block => ("content" in block ? String(block.content || "") : "")).join("");
    const last = blocks[blocks.length - 1];
    return { text, status: last.status === "streaming" ? "streaming" : "done" };
  }
  // Compatibility with hosts that only project `reasoning` (no ordered blocks).
  if (message.reasoning) return { text: message.reasoning, status: null };
  return { text: "", status: null };
}

/**
 * Live-only projection of the current turn's reasoning.
 *
 * History and the compacted transcript deliberately keep the summary-only
 * policy (`compactHarnessMessages`), so this returns null unless the trailing
 * model turn is actually streaming right now.  Detailed reasoning is never
 * persisted (see run_service private delta filter), which keeps the two views
 * independent: live streaming text vs. durable summary rows.
 */
export function extractLiveThinking(
  messages: Message[],
  options: { streaming: boolean },
): LiveThinking | null {
  if (!options.streaming) return null;
  let index = messages.length - 1;
  while (index >= 0 && messages[index].role !== "model") index -= 1;
  if (index < 0) return null;
  const runId = runIdentity(messages[index]);
  const turn: Message[] = [];
  for (let cursor = index; cursor >= 0; cursor -= 1) {
    const message = messages[cursor];
    if (message.role !== "model") break;
    const identity = runIdentity(message);
    if (runId && identity && identity !== runId) break;
    turn.unshift(message);
  }
  const parts: string[] = [];
  let status: "streaming" | "done" | null = null;
  for (const message of turn) {
    const thinking = thinkingOf(message);
    if (thinking.text) parts.push(thinking.text);
    if (thinking.status) status = thinking.status;
  }
  const text = parts.join("");
  if (!text.trim()) return null;
  // The answer closing the turn means reasoning is over even if a provider
  // left its last reasoning block marked as streaming.
  const answered = turn.some(message => Boolean(String(message.content || "").trim()));
  return { runId, text, streaming: status !== "done" && !answered };
}

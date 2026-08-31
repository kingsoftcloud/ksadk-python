import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkBreaks from "remark-breaks";
import remarkGfm from "remark-gfm";
import { Bot, BrainCircuit, MessageSquarePlus, PanelLeftOpen, ShieldAlert, ShieldCheck, Trash2, Wrench, X } from "lucide-react";
import { apiFetch } from "../api";
import {
  approvalModeStorageKey,
  normalizeApprovalMode,
  type ApprovalMode,
} from "../approvalModes";
import {
  parseComposerSubmission,
  type CollaborationMode,
  type ComposerCommand,
} from "../composerActions";
import {
  ChatComposer,
  type ComposerModelOption,
  type ReasoningEffort,
} from "./ChatComposer";
import { showToast } from "./Toast";
import { decodeConversationItem, type ConversationItem } from "../conversationProtocol";
import {
  rebuildPersistedSessionHistory,
  type ProcessingBlock,
} from "@kingsoftcloud/ksadk-web/conversation";

interface CloudChatWorkspaceProps {
  deploymentId: string;
  agentId: string;
  agentName: string;
  active?: boolean;
  refreshTick?: number;
}

interface CloudSession {
  id: string;
  title: string;
  updatedAt: string;
  state: string;
  error: string;
}

interface CloudMessage {
  id: string;
  role: "user" | "assistant" | "system";
  content: string;
  timestamp: string;
  pending?: boolean;
  streaming?: boolean;
  invocationId?: string;
  blocks?: ProcessingBlock[];
}

interface CloudInteraction {
  id: string;
  runId: string;
  revision: number;
  kind: string;
  title: string;
}

interface CloudModel {
  id: string;
  label: string;
  capabilities?: Record<string, unknown>;
}

interface RuntimeEnvelope {
  event: Record<string, unknown>;
  eventType: string;
  runId: string;
  invocationId: string;
  seq: number;
}

interface CloudRuntimeItem {
  id: string;
  kind: "message" | "reasoning" | "tool" | "approval" | "plan" | "goal" | "artifact" | "a2ui" | "error";
  title: string;
  text: string;
  detail: string;
  status: "running" | "waiting" | "completed" | "failed";
  operation: "append" | "replace";
  source?: "direct" | "session";
}

function explicitReasoningEfforts(model?: CloudModel): ReasoningEffort[] {
  const raw = model?.capabilities?.reasoning_efforts;
  if (!Array.isArray(raw)) return [];
  return raw.filter((value): value is ReasoningEffort => (
    value === "low" || value === "medium" || value === "high"
  ));
}

function valueText(value: unknown): string {
  if (typeof value === "string") return value;
  if (Array.isArray(value)) {
    return value.map(item => {
      if (typeof item === "string") return item;
      if (item && typeof item === "object") {
        const candidate = item as Record<string, unknown>;
        return valueText(candidate.text ?? candidate.content ?? candidate.value ?? "");
      }
      return "";
    }).filter(Boolean).join("\n");
  }
  if (value && typeof value === "object") {
    const candidate = value as Record<string, unknown>;
    return valueText(candidate.text ?? candidate.content ?? candidate.value ?? "");
  }
  return "";
}

function scalarText(value: unknown): string {
  return typeof value === "string" || typeof value === "number" ? String(value) : "";
}

function errorText(...values: unknown[]): string {
  for (const value of values) {
    if (typeof value === "string" && value.trim()) return value.trim();
    if (!value || typeof value !== "object") continue;
    const candidate = value as Record<string, unknown>;
    const nested = errorText(
      candidate.message,
      candidate.detail,
      candidate.reason,
      candidate.error,
      candidate.text,
    );
    if (nested) return nested;
  }
  return "";
}

function recordValue(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" ? value as Record<string, unknown> : {};
}

function runtimeEnvelope(value: unknown): RuntimeEnvelope | null {
  if (!value || typeof value !== "object") return null;
  const frame = value as Record<string, unknown>;
  const payload = Object.keys(recordValue(frame.payload)).length ? recordValue(frame.payload) : frame;
  const content = recordValue(payload.content);
  // Server-side SessionEvent history has used both snake_case and camelCase
  // during the RuntimeEvent/v2 rollout.  Treat those transport spellings as
  // the same envelope before any presentation fallback is considered.  A
  // provider event must not lose its identity merely because its enclosing
  // REST projection chose a different JSON casing.
  const nested = [
    content.runtime_event,
    content.runtimeEvent,
    payload.runtime_event,
    payload.runtimeEvent,
    frame.runtime_event,
    frame.runtimeEvent,
  ].map(recordValue).find(candidate => Object.keys(candidate).length > 0) || {};
  const event = Object.keys(nested).length ? nested : payload;
  const outerType = scalarText(frame.event_type ?? frame.eventType ?? payload.event_type ?? payload.eventType).toLowerCase();
  const nestedType = scalarText(event.event_type ?? event.eventType ?? event.type).toLowerCase();
  return {
    event,
    eventType: nestedType || outerType,
    runId: scalarText(
      event.run_id ?? event.runId ?? payload.run_id ?? payload.runId ?? frame.run_id ?? frame.runId,
    ),
    invocationId: scalarText(
      event.invocation_id ?? event.invocationId
      ?? payload.invocation_id ?? payload.invocationId
      ?? frame.invocation_id ?? frame.invocationId,
    ),
    seq: Number(
      event.seq ?? event.seq_id ?? event.source_session_seq
      ?? payload.seq ?? payload.seq_id ?? payload.source_session_seq
      ?? frame.seq ?? frame.seq_id ?? frame.source_session_seq ?? 0,
    ) || 0,
  };
}

function latestEventSeq(events: unknown[]): number {
  return events.reduce<number>((latest, event) => {
    const envelope = runtimeEnvelope(event);
    return Math.max(latest, envelope?.seq || 0);
  }, 0);
}

function itemPart(container: unknown): Record<string, unknown> {
  const record = recordValue(container);
  const parts = Array.isArray(record.parts) ? record.parts.map(recordValue) : [];
  return parts[0] || record;
}

function jsonDetail(value: unknown): string {
  if (typeof value === "string") return value;
  if (value === undefined || value === null) return "";
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

function conversationItemFromFrame(value: unknown): ConversationItem | null {
  const frame = recordValue(value);
  const payload = recordValue(frame.payload);
  const content = recordValue(payload.content);
  const event = runtimeEnvelope(value)?.event || {};
  const candidates = [
    frame.conversationItem,
    payload.conversationItem,
    content.conversationItem,
    event.conversationItem,
  ];
  for (const candidate of candidates) {
    const decoded = decodeConversationItem(candidate);
    if (decoded) return decoded;
  }
  return null;
}

function conversationItemPatch(value: unknown): CloudRuntimeItem | null {
  const item = conversationItemFromFrame(value);
  // Additive providers must not turn every unrecognised event into a chat
  // error.  The canonical projector makes them hidden; raw diagnostics remain
  // available in the run/event inspector.
  if (!item || item.visibility !== "public" || item.kind === "unknown" || item.kind === "progress") return null;
  const text = valueText(item.payload.text ?? item.payload.objective ?? item.payload.error ?? "");
  const status: CloudRuntimeItem["status"] = item.lifecycle === "failed"
    ? "failed"
    : item.lifecycle === "completed"
      ? "completed"
      : item.kind === "approval" && item.lifecycle === "pending"
        ? "waiting"
        : "running";
  const kind: CloudRuntimeItem["kind"] = item.kind === "assistant_text"
    ? "message"
    : item.kind === "reasoning"
      ? "reasoning"
      : item.kind === "tool_call"
        ? "tool"
        : item.kind;
  const title = valueText(
    item.payload.tool ?? item.payload.title ?? item.payload.name ?? item.payload.kind,
  ) || (kind === "reasoning" ? "思考过程"
    : kind === "approval" ? "等待确认"
      : kind === "plan" ? "计划"
        : kind === "goal" ? "目标"
          : kind === "artifact" ? "运行产物"
            : kind === "a2ui" ? "交互卡片"
              : kind === "error" ? "运行失败"
                : kind === "tool" ? "工具调用" : "回复");
  return {
    id: `${item.runId}/${item.itemId}`,
    kind,
    title,
    text,
    detail: text || jsonDetail(item.payload),
    status,
    operation: item.operation === "append" ? "append" : "replace",
  };
}

function runtimeItemPatch(value: unknown): CloudRuntimeItem | null {
  const typed = conversationItemPatch(value);
  if (typed) return typed;
  const envelope = runtimeEnvelope(value);
  if (!envelope) return null;
  const event = envelope.event;
  if (["message.delta", "response.output_text.delta", "output_text.delta"].includes(envelope.eventType)) {
    const content = recordValue(event.content);
    const update = recordValue(event.update);
    const text = valueText(update.delta ?? update.text ?? event.delta ?? content.delta ?? event.text);
    if (!text) return null;
    const itemId = scalarText(event.item_id ?? event.itemId ?? event.output_index) || "legacy";
    return {
      id: `${envelope.invocationId || envelope.runId}//message:${itemId}`,
      kind: "message",
      title: "回复",
      text,
      detail: text,
      status: "running",
      operation: event.replace === true || scalarText(event.op).toLowerCase() === "replace"
        ? "replace" : "append",
    };
  }
  if (!["item.started", "item.updated", "item.completed", "item.failed"].includes(envelope.eventType)) {
    return null;
  }
  const rawKind = scalarText(event.item_kind ?? event.itemKind).toLowerCase();
  const kind: CloudRuntimeItem["kind"] | null = ["message", "assistant", "assistant_message"].includes(rawKind)
    ? "message"
    : rawKind === "reasoning" ? "reasoning"
      : rawKind === "approval" ? "approval"
        : ["tool", "tool_call", "tool_result", "command", "command_execution"].includes(rawKind) ? "tool"
          : null;
  if (!kind) return null;
  const source = envelope.eventType === "item.started"
    ? event.initial
    : envelope.eventType === "item.completed" || envelope.eventType === "item.failed"
      ? event.snapshot
      : event.update;
  const sourceRecord = recordValue(source);
  const part = itemPart(source);
  const text = valueText(part.text ?? part.delta ?? part.content ?? sourceRecord.parts ?? event.text);
  const title = valueText(part.name ?? event.name ?? event.tool_name ?? event.title)
    || (kind === "reasoning" ? "思考过程" : kind === "approval" ? "等待确认" : kind === "tool" ? "工具调用" : "回复");
  const detail = text || jsonDetail(part.result ?? part.output ?? part.arguments ?? part.error ?? "");
  const itemId = scalarText(event.item_id ?? event.itemId ?? part.call_id ?? part.callId)
    || `${kind}:${title}`;
  return {
    id: [envelope.invocationId || envelope.runId, scalarText(event.scope_id ?? event.scopeId), itemId].join("/"),
    kind,
    title,
    text,
    detail,
    status: envelope.eventType === "item.failed" ? "failed"
      : kind === "approval" && envelope.eventType !== "item.completed" ? "waiting"
        : envelope.eventType === "item.completed" ? "completed" : "running",
    operation: scalarText(event.op).toLowerCase() === "replace"
      ? "replace"
      : envelope.eventType === "item.updated"
        ? "append"
        : "replace",
  };
}

function directStreamItemPatches(value: unknown): CloudRuntimeItem[] {
  const canonical = runtimeItemPatch(value);
  if (canonical) return [canonical];
  const envelope = runtimeEnvelope(value);
  if (!envelope) return [];
  const event = envelope.event;
  const eventType = envelope.eventType;
  const streamId = scalarText(event.id ?? event.response_id ?? event.responseId)
    || envelope.invocationId || envelope.runId || "direct";
  const patches: CloudRuntimeItem[] = [];

  const choices = Array.isArray(event.choices) ? event.choices.map(recordValue) : [];
  choices.forEach((choice, choiceIndex) => {
    const delta = recordValue(choice.delta);
    const reasoning = valueText(delta.reasoning_content ?? delta.reasoning ?? delta.thinking);
    if (reasoning) {
      patches.push({
        id: `${streamId}//reasoning:${scalarText(choice.index) || choiceIndex}`,
        kind: "reasoning",
        title: "思考过程",
        text: reasoning,
        detail: reasoning,
        status: "running",
        operation: "append",
      });
    }
    const content = valueText(delta.content);
    if (content) {
      patches.push({
        id: `${streamId}//message:${scalarText(choice.index) || choiceIndex}`,
        kind: "message",
        title: "回复",
        text: content,
        detail: content,
        status: "running",
        operation: "append",
      });
    }
    const toolCalls = Array.isArray(delta.tool_calls) ? delta.tool_calls.map(recordValue) : [];
    toolCalls.forEach((call, callIndex) => {
      const callable = recordValue(call.function);
      const title = valueText(callable.name ?? call.name) || "工具调用";
      const detail = valueText(callable.arguments ?? call.arguments);
      patches.push({
        id: `${streamId}//tool:${scalarText(choice.index) || choiceIndex}:${scalarText(call.index) || callIndex}`,
        kind: "tool",
        title,
        text: "",
        detail,
        status: "running",
        operation: "append",
      });
    });
  });

  // Historical RunAgent deployments stream assistant text as a minimal
  // `{"delta":"..."}` frame before returning the terminal Responses object.
  // The direct stream has no item id in that shape, so keep one stable item
  // per foreground request and append each fragment as it arrives.  Typed
  // Responses events also carry `delta`, but have an event type and are
  // handled by their dedicated branches below.
  if (!eventType && choices.length === 0 && typeof event.delta === "string" && event.delta) {
    patches.push({
      id: `${streamId}//message:0`,
      kind: "message",
      title: "回复",
      text: event.delta,
      detail: event.delta,
      status: "running",
      operation: "append",
    });
  }

  if (eventType.includes("reasoning") && eventType.endsWith(".delta")) {
    const text = valueText(event.delta ?? event.text ?? recordValue(event.part).text);
    if (text) {
      patches.push({
        id: `${streamId}//reasoning:${scalarText(event.item_id ?? event.itemId) || "summary"}`,
        kind: "reasoning",
        title: "思考过程",
        text,
        detail: text,
        status: "running",
        operation: event.replace === true || scalarText(event.op).toLowerCase() === "replace"
          ? "replace" : "append",
      });
    }
  }

  if (["response.output_item.added", "response.output_item.done"].includes(eventType)) {
    const item = recordValue(event.item);
    const itemType = scalarText(item.type).toLowerCase();
    if (["mcp_approval_request", "approval_request"].includes(itemType)) {
      patches.push({
        id: `${streamId}//approval:${scalarText(item.id ?? item.approval_request_id ?? item.call_id) || "request"}`,
        kind: "approval",
        title: valueText(item.title ?? item.name ?? item.server_label) || "等待确认",
        text: "",
        detail: valueText(item.message) || jsonDetail(item.arguments ?? item.request ?? ""),
        status: "waiting",
        operation: "replace",
      });
    }
    if (["function_call", "tool_call", "computer_call", "mcp_call"].includes(itemType)) {
      patches.push({
        id: `${streamId}//tool:${scalarText(item.id ?? item.call_id ?? event.output_index) || "output"}`,
        kind: "tool",
        title: valueText(item.name) || "工具调用",
        text: "",
        detail: valueText(item.arguments ?? item.output) || jsonDetail(item.arguments ?? item.output ?? ""),
        status: eventType.endsWith(".done") ? "completed" : "running",
        operation: "replace",
      });
    }
  }

  if (["response.function_call_arguments.delta", "response.mcp_call_arguments.delta"].includes(eventType)) {
    const detail = valueText(event.delta);
    patches.push({
      id: `${streamId}//tool:${scalarText(event.item_id ?? event.itemId ?? event.call_id) || "output"}`,
      kind: "tool",
      title: valueText(event.name) || "工具调用",
      text: "",
      detail,
      status: "running",
      operation: "append",
    });
  }
  if (eventType === "response.approval_request") {
    patches.push({
      id: `${streamId}//approval:${scalarText(event.interaction_id ?? event.approval_request_id ?? event.item_id) || "request"}`,
      kind: "approval",
      title: valueText(event.title ?? event.message ?? recordValue(event.request).title) || "等待确认",
      text: "",
      detail: valueText(event.message) || jsonDetail(event.request ?? ""),
      status: "waiting",
      operation: "replace",
    });
  }
  return patches;
}

function streamEventIdentity(value: unknown): string {
  const envelope = runtimeEnvelope(value);
  if (!envelope) return "";
  const eventId = scalarText(envelope.event.event_id ?? envelope.event.eventId);
  if (!eventId) return "";
  return `${envelope.runId || envelope.invocationId}/${eventId}`;
}

function directStreamTerminal(value: unknown): TerminalRunResult | null {
  const envelope = runtimeEnvelope(value);
  if (!envelope) return null;
  const event = envelope.event;
  const eventType = envelope.eventType;
  if (scalarText(event.object).toLowerCase() === "response") {
    const status = scalarText(event.status).toLowerCase();
    if (status === "completed") return { status: "completed", error: "" };
    if (["failed", "cancelled", "canceled", "incomplete"].includes(status)) {
      return {
        status: "failed",
        error: errorText(event.error, event.incomplete_details) || "云端流式响应失败",
      };
    }
  }
  if (["stream.done", "response.completed", "response.done", "done"].includes(eventType)) {
    return { status: "completed", error: "" };
  }
  if (event.error || ["error", "stream.error", "response.failed", "response.error"].includes(eventType)) {
    return {
      status: "failed",
      error: errorText(event.error, event.response, event.message, event.detail)
        || "云端流式响应失败",
    };
  }
  return null;
}

function mergeRuntimeItem(items: CloudRuntimeItem[], patch: CloudRuntimeItem): CloudRuntimeItem[] {
  const index = items.findIndex(item => item.id === patch.id);
  if (index < 0) return [...items, patch];
  const previous = items[index];
  const next = [...items];
  next[index] = {
    ...previous,
    ...patch,
    title: patch.title === "回复" || patch.title === "思考过程" || patch.title === "工具调用"
      ? previous.title : patch.title,
    text: patch.operation === "append" ? `${previous.text}${patch.text}` : patch.text || previous.text,
    detail: patch.kind === "tool" && patch.operation === "append"
      ? `${previous.detail}${patch.detail}`
      : patch.detail || previous.detail,
  };
  return next;
}

async function loadCompleteCloudSessionEvents(base: string, sessionId: string): Promise<unknown[]> {
  const path = `${base}/sessions/${encodeURIComponent(sessionId)}/events`;
  const firstResponse = await apiFetch(`${path}?limit=1000`);
  if (!firstResponse.ok) throw new Error(await responseError(firstResponse));
  const firstPayload = await firstResponse.json() as Record<string, unknown>;
  const events = Array.isArray(firstPayload.events) ? [...firstPayload.events] : [];
  const total = Number(firstPayload.total ?? firstPayload.Total ?? events.length);
  let offset = events.length;
  while (offset < total) {
    const response = await apiFetch(`${path}?limit=1000&offset=${offset}`);
    if (!response.ok) throw new Error(await responseError(response));
    const payload = await response.json() as Record<string, unknown>;
    const page = Array.isArray(payload.events) ? payload.events : [];
    if (!page.length) break;
    events.push(...page);
    offset += page.length;
  }
  return events;
}

function normalizeSession(value: unknown): CloudSession | null {
  if (!value || typeof value !== "object") return null;
  const item = value as Record<string, unknown>;
  const id = String(item.session_id ?? item.sessionId ?? item.id ?? "").trim();
  if (!id) return null;
  return {
    id,
    title: valueText(item.title ?? item.summary ?? item.first_prompt ?? "") || "新会话",
    updatedAt: scalarText(item.updated_at ?? item.updatedAt ?? item.created_at),
    state: scalarText(item.active_run_status ?? item.state),
    error: errorText(
      item.active_run_error,
      item.activeRunError,
      item.last_error,
      item.lastError,
      item.error,
    ),
  };
}

function cloudSessionActivity(state: string): "running" | "waiting_input" | "failed" | null {
  const normalized = state.trim().toLowerCase();
  if (["running", "streaming", "queued", "pending", "accepted"].includes(normalized)) return "running";
  if (["paused", "waiting", "waiting_input", "requires_action"].includes(normalized)) return "waiting_input";
  if (["failed", "error", "cancelled", "canceled", "expired", "aborted"].includes(normalized)) return "failed";
  return null;
}

function normalizeMessage(value: unknown): CloudMessage | null {
  if (!value || typeof value !== "object") return null;
  const item = value as Record<string, unknown>;
  const rawRole = String(item.role ?? "assistant").toLowerCase();
  const role = rawRole === "user" || rawRole === "system" ? rawRole : "assistant";
  return {
    id: String(item.message_id ?? item.messageId ?? item.seq_id ?? crypto.randomUUID()),
    role,
    content: valueText(item.content),
    timestamp: String(item.timestamp ?? ""),
    invocationId: scalarText(item.invocation_id ?? item.invocationId ?? item.run_id ?? item.runId),
  };
}

function canonicalSessionEvent(value: unknown): Record<string, unknown> {
  const event = recordValue(value);
  return {
    ...event,
    SeqId: event.SeqId ?? event.seq_id ?? event.seqId ?? 0,
    EventId: event.EventId ?? event.event_id ?? event.eventId ?? "",
    EventType: event.EventType ?? event.event_type ?? event.eventType ?? "",
    InvocationId: event.InvocationId ?? event.invocation_id ?? event.invocationId ?? event.run_id ?? event.runId ?? "",
    Timestamp: event.Timestamp ?? event.timestamp ?? "",
    Content: event.Content ?? event.content ?? {},
    Metadata: event.Metadata ?? event.metadata ?? {},
  };
}

function canonicalCloudHistory(
  fallback: CloudMessage[],
  events: unknown[],
  sessionId: string,
): { messages: CloudMessage[]; canonicalRunIds: Set<string> } {
  if (!events.length) return { messages: fallback, canonicalRunIds: new Set() };
  const compatibleFallback = fallback.map((message, index) => ({
    id: message.id,
    role: message.role === "assistant" ? "model" : message.role,
    content: message.content,
    timestamp: Number.isFinite(Date.parse(message.timestamp))
      ? Date.parse(message.timestamp)
      : index,
    invocationId: message.invocationId || undefined,
  })) as Parameters<typeof rebuildPersistedSessionHistory>[0];
  const rebuilt = rebuildPersistedSessionHistory(
    compatibleFallback,
    events.map(canonicalSessionEvent) as Parameters<typeof rebuildPersistedSessionHistory>[1],
    sessionId,
  );
  const messages = rebuilt.messages
    .filter(message => message.role === "user" || message.role === "model" || message.role === "system")
    .map(message => ({
      id: message.id,
      role: message.role === "model" ? "assistant" : message.role,
      content: message.content,
      timestamp: new Date(Number(message.timestamp) || 0).toISOString(),
      invocationId: message.invocationId,
      blocks: message.blocks,
    }));
  return { messages, canonicalRunIds: new Set(rebuilt.canonicalRunIds) };
}

function canonicalCloudMessages(
  fallback: CloudMessage[],
  events: unknown[],
  sessionId: string,
): CloudMessage[] {
  return canonicalCloudHistory(fallback, events, sessionId).messages;
}

function pendingInteractions(events: unknown[]): CloudInteraction[] {
  const requested = new Map<string, CloudInteraction>();
  for (const event of events) {
    const envelope = runtimeEnvelope(event);
    if (!envelope) continue;
    const payload = envelope.event;
    const frame = event as Record<string, unknown>;
    const eventType = envelope.eventType;
    const metadata = payload.Metadata && typeof payload.Metadata === "object"
      ? payload.Metadata as Record<string, unknown>
      : payload.metadata && typeof payload.metadata === "object"
        ? payload.metadata as Record<string, unknown>
        : frame.Metadata && typeof frame.Metadata === "object"
          ? frame.Metadata as Record<string, unknown>
          : frame.metadata && typeof frame.metadata === "object"
            ? frame.metadata as Record<string, unknown>
        : {};
    const interruptInfo = metadata.interrupt_info && typeof metadata.interrupt_info === "object"
      ? metadata.interrupt_info as Record<string, unknown>
      : {};
    const resumeInput = metadata.resume_input && typeof metadata.resume_input === "object"
      ? metadata.resume_input as Record<string, unknown>
      : {};
    const interactionId = String(
      payload.interaction_id
      ?? payload.interactionId
      ?? interruptInfo.approval_request_id
      ?? resumeInput.approval_request_id
      ?? "",
    ).trim();
    if (!interactionId) continue;
    if (["interaction.requested", "approval_request", "response.approval_request"].includes(eventType)) {
      const request = payload.request && typeof payload.request === "object"
        ? payload.request as Record<string, unknown>
        : {};
      requested.set(interactionId, {
        id: interactionId,
        runId: String(
          payload.run_id
          ?? payload.runId
          ?? envelope.invocationId
          ?? envelope.runId
          ?? frame.run_id
          ?? frame.runId
          ?? frame.InvocationId
          ?? frame.invocation_id
          ?? "",
        ),
        revision: Number(payload.revision ?? 1) || 1,
        kind: String(payload.interaction_kind ?? payload.interactionKind ?? payload.kind ?? request.kind
          ?? (["approval_request", "response.approval_request"].includes(eventType) ? "approval" : "input")),
        title: valueText(
          request.title
          ?? request.message
          ?? request.prompt
          ?? interruptInfo.approval_message
          ?? interruptInfo.tool_name
          ?? request.kind
          ?? "需要你的确认",
        ) || "需要你的确认",
      });
    } else if (["interaction.resolved", "interaction.cancelled", "interaction.expired", "approval_response"].includes(eventType)) {
      requested.delete(interactionId);
    }
  }
  return [...requested.values()];
}

interface TerminalRunResult {
  status: "completed" | "failed" | "interrupted";
  error: string;
}

function terminalRunEvent(
  events: unknown[],
  runId: string,
  invocationId: string,
  afterSeq: number,
): TerminalRunResult | null {
  if (!runId && !invocationId && afterSeq <= 0) return null;
  for (const event of events) {
    const envelope = runtimeEnvelope(event);
    if (!envelope) continue;
    const payload = envelope.event;
    const eventRunId = envelope.runId || envelope.invocationId;
    const eventSeq = envelope.seq;
    // The current pre-production Server projection exposes the admitted
    // Runtime run id in the receipt but still labels historical events with
    // the outer invocation id.  Prefer an exact id match, then fall back to
    // the receipt's accepted Session sequence.  The composer admits one run
    // at a time, so the sequence window remains unambiguous for this client.
    const matchesRun = Boolean(eventRunId) && [runId, invocationId].filter(Boolean).includes(eventRunId);
    const matchesAcceptedWindow = afterSeq > 0 && eventSeq > afterSeq;
    if (!matchesRun && !matchesAcceptedWindow) continue;
    const eventType = envelope.eventType;
    const content = payload.content && typeof payload.content === "object"
      ? payload.content as Record<string, unknown>
      : {};
    const failure = errorText(
      payload.error,
      payload.message,
      content.error,
      content.message,
      content.detail,
    );
    if (["run.completed", "run.complete", "run.succeeded"].includes(eventType)) {
      return { status: "completed", error: "" };
    }
    if (["run.interrupted", "run.paused", "run.waiting_input", "run.requires_action"].includes(eventType)) {
      return { status: "interrupted", error: "" };
    }
    if (["run.failed", "run.cancelled", "run.expired", "run.error"].includes(eventType)) {
      return { status: "failed", error: failure };
    }
    if (["run_status", "run.status"].includes(eventType)) {
      const stateDelta = payload.state_delta && typeof payload.state_delta === "object"
        ? payload.state_delta as Record<string, unknown>
        : {};
      const activeRun = stateDelta.active_run && typeof stateDelta.active_run === "object"
        ? stateDelta.active_run as Record<string, unknown>
        : {};
      const status = String(payload.status ?? content.status ?? activeRun.status ?? "").toLowerCase();
      if (["completed", "complete", "succeeded", "success"].includes(status)) {
        return { status: "completed", error: "" };
      }
      if (["interrupted", "paused", "waiting", "waiting_input", "requires_action"].includes(status)) {
        return { status: "interrupted", error: "" };
      }
      if (["failed", "cancelled", "canceled", "expired", "error", "aborted"].includes(status)) {
        return {
          status: "failed",
          error: failure || errorText(activeRun.error, activeRun.message, activeRun.reason),
        };
      }
    }
  }
  return null;
}

async function consumeSseResponse(
  response: Response,
  onFrame: (frame: unknown) => void,
  signal: AbortSignal,
): Promise<void> {
  if (!response.ok) throw new Error(await responseError(response));
  if (!response.body) throw new Error("云端事件流为空");
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  const cancel = () => { reader.cancel().catch(() => {}); };
  const consumeFrame = (rawFrame: string) => {
    const lines = rawFrame.split(/\r?\n/);
    const eventName = lines.find(line => line.startsWith("event:"))?.slice(6).trim() || "";
    const data = lines
      .filter(line => line.startsWith("data:"))
      .map(line => line.slice(5).trimStart())
      .join("\n");
    if (!data) return;
    if (data === "[DONE]") {
      onFrame({ event_type: "stream.done" });
      return;
    }
    try {
      const parsed = JSON.parse(data);
      if (eventName && parsed && typeof parsed === "object") {
        const record = parsed as Record<string, unknown>;
        onFrame(record.event_type || record.eventType || record.type
          ? record
          : { ...record, event_type: eventName });
      } else {
        onFrame(parsed);
      }
    } catch {
      // Ignore a malformed frame and let the authoritative message poll
      // reconcile the conversation instead of terminating the stream.
    }
  };
  signal.addEventListener("abort", cancel, { once: true });
  try {
    while (!signal.aborted) {
      const { done, value } = await reader.read();
      buffer += decoder.decode(value, { stream: !done });
      const frames = buffer.split(/\r?\n\r?\n/);
      buffer = frames.pop() || "";
      frames.forEach(consumeFrame);
      if (done) {
        if (buffer.trim()) consumeFrame(buffer);
        break;
      }
    }
  } finally {
    signal.removeEventListener("abort", cancel);
    reader.releaseLock();
  }
}

async function responseError(response: Response): Promise<string> {
  try {
    const body = await response.json();
    return String(body?.error?.message || body?.message || body?.detail || `请求失败 (${response.status})`);
  } catch {
    return `请求失败 (${response.status})`;
  }
}

async function fileDataUrl(file: File): Promise<string> {
  if (file.size > 10 * 1024 * 1024) throw new Error(`${file.name} 超过 10 MB 限制`);
  return await new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result || ""));
    reader.onerror = () => reject(new Error(`${file.name} 读取失败`));
    reader.readAsDataURL(file);
  });
}

function CloudThinkingBlock({ content, running }: { content: string; running: boolean }) {
  return (
    <details className="chat-processing-group" open={running} data-ui="think">
      <summary>
        <BrainCircuit size={15} className="chat-processing-icon" />
        <span className={running ? "text-shimmer" : ""}>{running ? "正在思考" : "已思考"}</span>
      </summary>
      <div className="chat-processing-content">
        <div className="chat-reasoning-content">{content}</div>
      </div>
    </details>
  );
}

function CloudActivityBlock({ item }: { item: CloudRuntimeItem }) {
  return (
    <div className={`chat-activity-card ${item.kind}`}>
      <div className="chat-activity-row">
        <span className="chat-activity-icon">{item.kind === "approval" ? <ShieldAlert size={15} /> : item.kind === "plan" || item.kind === "goal" ? <BrainCircuit size={15} /> : <Wrench size={15} />}</span>
        <span className="chat-activity-copy"><small>{item.kind === "approval" ? "批准" : item.kind === "plan" ? "计划" : item.kind === "goal" ? "目标" : item.kind === "artifact" ? "产物" : item.kind === "a2ui" ? "交互" : item.kind === "error" ? "错误" : "工具"}</small><strong>{item.title}</strong></span>
        <span className={`chat-activity-status ${item.status}`}>
          {item.status === "completed" ? "已完成" : item.status === "failed" ? "失败" : item.status === "waiting" ? "等待确认" : "运行中"}
        </span>
      </div>
      {item.detail && <pre>{item.detail}</pre>}
    </div>
  );
}

function CloudMessageBody({ message }: { message: CloudMessage }) {
  const blocks = message.role === "assistant" ? message.blocks || [] : [];
  if (!blocks.length) {
    return <div className="message-content"><ReactMarkdown remarkPlugins={[remarkGfm, remarkBreaks]}>{message.content || "…"}</ReactMarkdown></div>;
  }
  return (
    <div className="message-blocks">
      {blocks.map(block => {
        if (block.type === "thinking") {
          return <CloudThinkingBlock key={block.id} content={block.content} running={block.status === "streaming"} />;
        }
        if (block.type === "tool") {
          return (
            <CloudActivityBlock
              key={block.id}
              item={{
                id: block.id,
                kind: "tool",
                title: block.toolName || "工具调用",
                text: "",
                detail: block.output || block.args,
                status: block.status === "error" ? "failed" : block.status === "paused" ? "waiting" : block.status === "completed" ? "completed" : "running",
                operation: "replace",
              }}
            />
          );
        }
        return <div className="message-content" key={block.id}><ReactMarkdown remarkPlugins={[remarkGfm, remarkBreaks]}>{block.content}</ReactMarkdown></div>;
      })}
    </div>
  );
}

function CloudRuntimeTimeline({ items, agentName, streaming }: { items: CloudRuntimeItem[]; agentName: string; streaming: boolean }) {
  if (!items.length) return null;
  const canonicalMessages = items.filter(item => item.kind === "message" && item.source === "session");
  const directMessages = items.filter(item => item.kind === "message" && item.source === "direct");
  let visibleItems = items;
  if (canonicalMessages.length && directMessages.length) {
    const canonicalText = canonicalMessages.map(item => item.text).join("");
    const directText = directMessages.map(item => item.text).join("");
    let sharedPrefix = 0;
    while (sharedPrefix < canonicalText.length
      && sharedPrefix < directText.length
      && canonicalText[sharedPrefix] === directText[sharedPrefix]) sharedPrefix += 1;
    const directRemainder = directText.slice(sharedPrefix);
    visibleItems = items.filter(item => !(item.kind === "message" && item.source === "direct"));
    if (directRemainder) {
      visibleItems = [...visibleItems, {
        ...directMessages[0],
        id: `${directMessages[0].id}:remainder`,
        text: directRemainder,
        detail: directRemainder,
      }];
    }
  }
  return (
    <div className="cloud-runtime-timeline" data-ui="runtime-timeline">
      {visibleItems.map(item => item.kind === "message" ? (
        <article key={item.id} className="message assistant streaming" aria-label="云端流式回复">
          <div className="message-meta">{agentName}</div>
          <div className="message-content"><ReactMarkdown remarkPlugins={[remarkGfm, remarkBreaks]}>{item.text}</ReactMarkdown></div>
        </article>
      ) : item.kind === "reasoning" ? (
        <CloudThinkingBlock key={item.id} content={item.text} running={streaming && item.status === "running"} />
      ) : (
        <CloudActivityBlock key={item.id} item={item} />
      ))}
    </div>
  );
}

export function CloudChatWorkspace({
  deploymentId,
  agentId,
  agentName,
  active = true,
  refreshTick = 0,
}: CloudChatWorkspaceProps) {
  const [sessions, setSessions] = useState<CloudSession[]>([]);
  const [currentSessionId, setCurrentSessionId] = useState("");
  const [messages, setMessages] = useState<CloudMessage[]>([]);
  const [streamingRuntimeItems, setStreamingRuntimeItems] = useState<CloudRuntimeItem[]>([]);
  const [interactions, setInteractions] = useState<CloudInteraction[]>([]);
  const [input, setInput] = useState("");
  const [attachments, setAttachments] = useState<File[]>([]);
  const [models, setModels] = useState<CloudModel[]>([]);
  const [selectedModel, setSelectedModel] = useState("");
  const [approvalMode, setApprovalMode] = useState<ApprovalMode>("risk");
  const [collaborationMode, setCollaborationMode] = useState<CollaborationMode>("default");
  const [reasoningEffort, setReasoningEffort] = useState<ReasoningEffort>("");
  const [commandIndex, setCommandIndex] = useState(0);
  const [loading, setLoading] = useState(true);
  const [sending, setSending] = useState(false);
  const [waitingForResponse, setWaitingForResponse] = useState(false);
  const [runError, setRunError] = useState("");
  const [deleting, setDeleting] = useState("");
  const [resolvingInteractionId, setResolvingInteractionId] = useState("");
  const [sessionPanelOpen, setSessionPanelOpen] = useState(false);
  const messageListRef = useRef<HTMLDivElement>(null);
  const currentSessionIdRef = useRef("");
  const waitingForResponseRef = useRef(false);
  const assistantIdsBeforeSendRef = useRef<Set<string>>(new Set());
  const awaitingRunIdRef = useRef("");
  const awaitingInvocationIdRef = useRef("");
  const awaitingAcceptedSeqRef = useRef(0);
  const sessionCursorRef = useRef<Map<string, number>>(new Map());
  const sendInFlightRef = useRef(false);
  const streamAbortRef = useRef<AbortController | null>(null);
  const streamedFramesRef = useRef<unknown[]>([]);
  const directStreamActiveRef = useRef(false);
  // RunAgent and the durable SessionEvent stream can carry the same canonical
  // RuntimeEvent concurrently. De-duplicate the event, never the item kind or
  // item id: one item legitimately receives many distinct delta events.
  const projectedStreamEventIdsRef = useRef<Set<string>>(new Set());
  const followTailRef = useRef(true);
  const fallbackMessagesRef = useRef<CloudMessage[]>([]);
  const durableEventsRef = useRef<unknown[]>([]);

  const base = useMemo(
    () => `/api/v1/deployments/${encodeURIComponent(deploymentId)}/cloud-chat`,
    [deploymentId],
  );

  const settleCloudRun = useCallback((error = "", title = "云端运行未完成") => {
    const wasWaiting = waitingForResponseRef.current;
    waitingForResponseRef.current = false;
    directStreamActiveRef.current = false;
    setWaitingForResponse(false);
    awaitingRunIdRef.current = "";
    awaitingInvocationIdRef.current = "";
    awaitingAcceptedSeqRef.current = 0;
    setMessages(previous => previous.map(item => item.pending ? { ...item, pending: false } : item));
    streamAbortRef.current?.abort();
    streamAbortRef.current = null;
    if (error) {
      setRunError(error);
      if (wasWaiting) showToast(title, error, "error");
    }
  }, []);

  const refreshSessions = useCallback(async (selectFallback = true) => {
    const response = await apiFetch(`${base}/sessions`);
    if (!response.ok) throw new Error(await responseError(response));
    const payload = await response.json() as { sessions?: unknown[]; items?: unknown[] };
    const rows = (payload.sessions || payload.items || [])
      .map(normalizeSession)
      .filter((item: CloudSession | null): item is CloudSession => Boolean(item));
    setSessions(rows);
    const selected = rows.find(item => item.id === currentSessionIdRef.current);
    if (selected && cloudSessionActivity(selected.state) === "failed") {
      settleCloudRun(
        selected.error || "这次云端运行未完成；可新建会话后重试。若持续失败，请到可观测页面按会话查看记录。",
      );
    }
    setCurrentSessionId(previous => {
      const next = rows.some(item => item.id === previous)
        ? previous
        : selectFallback ? rows[0]?.id || "" : "";
      currentSessionIdRef.current = next;
      return next;
    });
  }, [base, settleCloudRun]);

  const refreshMessages = useCallback(async (sessionId: string) => {
    if (!sessionId) {
      setMessages([]);
      return;
    }
    const response = await apiFetch(`${base}/sessions/${encodeURIComponent(sessionId)}/messages`);
    if (!response.ok) throw new Error(await responseError(response));
    const payload = await response.json() as { messages?: unknown[] };
    if (currentSessionIdRef.current !== sessionId) return;
    const rows = (payload.messages || [])
      .map(normalizeMessage)
      .filter((item: CloudMessage | null): item is CloudMessage => Boolean(item));
    fallbackMessagesRef.current = rows;
    const rebuiltRows = canonicalCloudMessages(rows, durableEventsRef.current, sessionId);
    setMessages(rebuiltRows);
    const hasNewAssistant = rebuiltRows.some(
      message => message.role === "assistant" && !assistantIdsBeforeSendRef.current.has(message.id),
    );
    const durableRunIds = new Set(durableEventsRef.current.flatMap(event => {
      const envelope = runtimeEnvelope(event);
      return envelope ? [envelope.runId, envelope.invocationId].filter(Boolean) : [];
    }));
    const canonicalCaughtUp = rebuiltRows.some(message => (
      message.role === "assistant"
      && !assistantIdsBeforeSendRef.current.has(message.id)
      && Boolean(message.invocationId && durableRunIds.has(message.invocationId))
      && Boolean(message.blocks?.length)
    ));
    if (hasNewAssistant) {
      // ListSessionMessages may land before canonical RuntimeEvents. Keep
      // transient reasoning/tool order until the shared reducer can own the
      // complete run, but never render the streamed assistant body twice.
      setStreamingRuntimeItems(previous => canonicalCaughtUp
        ? []
        : previous.filter(item => item.kind !== "message"));
      if (!directStreamActiveRef.current) {
        streamedFramesRef.current = [];
      }
    }
    if (waitingForResponseRef.current && hasNewAssistant && !directStreamActiveRef.current) {
      setRunError("");
      settleCloudRun();
    }
  }, [base, settleCloudRun]);

  const refreshInteractions = useCallback(async (sessionId: string) => {
    if (!sessionId) {
      setInteractions([]);
      return;
    }
    // A single Codex turn can easily exceed the Server's default 200-event
    // window because text and reasoning deltas are canonical RuntimeEvents.
    // Read the complete supported history window so a reload cannot discard
    // the reasoning/tool items that precede a long assistant response.
    const events = await loadCompleteCloudSessionEvents(base, sessionId);
    if (currentSessionIdRef.current !== sessionId) return;
    durableEventsRef.current = events;
    const canonicalHistory = canonicalCloudHistory(
      fallbackMessagesRef.current,
      events,
      sessionId,
    );
    const rebuiltRows = canonicalHistory.messages;
    setMessages(rebuiltRows);
    sessionCursorRef.current.set(
      sessionId,
      Math.max(sessionCursorRef.current.get(sessionId) || 0, latestEventSeq(events)),
    );
    const terminal = terminalRunEvent(
      events,
      awaitingRunIdRef.current,
      awaitingInvocationIdRef.current,
      awaitingAcceptedSeqRef.current,
    );
    if (terminal && (!directStreamActiveRef.current || terminal.status !== "completed")) {
      settleCloudRun(terminal.status === "failed"
        ? terminal.error || "本次请求已结束，未得到回复。可新建会话后重试；若持续失败，请到可观测页面按会话查看记录。"
        : "");
    }
    // The foreground stream can surface an approval before the durable
    // SessionEvent projection catches up. Preserve those frames during that
    // window; later resolved/cancelled history is appended and removes it.
    const interactionFrames = [...streamedFramesRef.current, ...events].slice(-500);
    streamedFramesRef.current = interactionFrames;
    setInteractions(pendingInteractions(interactionFrames));
    const durableRunIds = new Set(events.flatMap(event => {
      const envelope = runtimeEnvelope(event);
      return envelope ? [envelope.runId, envelope.invocationId].filter(Boolean) : [];
    }));
    const canonicalCaughtUp = rebuiltRows.some(message => (
      message.role === "assistant"
      && !assistantIdsBeforeSendRef.current.has(message.id)
      && Boolean(message.invocationId && durableRunIds.has(message.invocationId))
      && Boolean(message.blocks?.length)
    ));
    const durableCompatibilityItems = events.reduce<CloudRuntimeItem[]>((items, event) => {
        const patch = runtimeItemPatch(event);
        if (!patch || patch.kind === "message") return items;
        const envelope = runtimeEnvelope(event);
        const runId = envelope?.runId || envelope?.invocationId || "";
        const runBlocks = rebuiltRows
          .filter(message => message.invocationId === runId)
          .flatMap(message => message.blocks || []);
        const ownedByCanonicalBlocks = patch.kind === "reasoning"
          ? runBlocks.some(block => block.type === "thinking")
          : patch.kind === "tool"
            ? runBlocks.some(block => block.type === "tool" && block.toolName === patch.title)
            : false;
        // Suppress a compatibility card only when the shared reducer really
        // materialised the same block. Run-level membership alone is not
        // enough: older RuntimeEvent producers can mix canonical messages
        // with legacy tool projections in one run.
        if (ownedByCanonicalBlocks) return items;
        return mergeRuntimeItem(items, patch);
    }, []);
    if (canonicalCaughtUp || (!directStreamActiveRef.current && durableCompatibilityItems.length)) {
      setStreamingRuntimeItems(previous => {
        if (canonicalCaughtUp) return durableCompatibilityItems;
        // A clean foreground EOF can precede both durable projections.
        // Never replace the only visible streamed answer with older history.
        if (previous.some(item => item.kind === "message")) return previous;
        return durableCompatibilityItems;
      });
    }
  }, [base, settleCloudRun]);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setSessions([]);
    setCurrentSessionId("");
    currentSessionIdRef.current = "";
    setMessages([]);
    fallbackMessagesRef.current = [];
    durableEventsRef.current = [];
    setStreamingRuntimeItems([]);
    streamedFramesRef.current = [];
    setInteractions([]);
    setRunError("");
    waitingForResponseRef.current = false;
    directStreamActiveRef.current = false;
    projectedStreamEventIdsRef.current = new Set();
    setWaitingForResponse(false);
    awaitingRunIdRef.current = "";
    awaitingInvocationIdRef.current = "";
    awaitingAcceptedSeqRef.current = 0;
    sessionCursorRef.current.clear();
    refreshSessions()
      .catch(error => { if (!cancelled) showToast("云端会话加载失败", error.message, "error"); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [refreshSessions, refreshTick]);

  useEffect(() => () => {
    streamAbortRef.current?.abort();
    streamAbortRef.current = null;
  }, []);

  useEffect(() => {
    let cancelled = false;
    apiFetch(`${base}/models`)
      .then(async response => {
        if (!response.ok) throw new Error(await responseError(response));
        return await response.json() as Record<string, unknown>;
      })
      .then(payload => {
        if (cancelled) return;
        const rawModels = Array.isArray(payload.models) ? payload.models : Array.isArray(payload.items) ? payload.items : [];
        const normalized = rawModels.map(item => {
          if (typeof item === "string") return { id: item, label: item };
          if (!item || typeof item !== "object") return null;
          const model = item as Record<string, unknown>;
          const id = String(model.id ?? model.model ?? model.name ?? "").trim();
          const capabilities = model.capabilities && typeof model.capabilities === "object"
            ? model.capabilities as Record<string, unknown>
            : undefined;
          return id ? {
            id,
            label: String(model.display_name ?? model.displayName ?? model.label ?? id),
            capabilities,
          } : null;
        }).filter((item): item is CloudModel => Boolean(item));
        const current = String(payload.current ?? payload.configured_model ?? payload.configuredModel ?? "").trim();
        setModels(normalized);
        setSelectedModel(previous => previous || (normalized.some(item => item.id === current) ? current : normalized[0]?.id || current));
      })
      .catch(() => {
        // Model discovery is optional; the deployed manifest default remains authoritative.
      });
    return () => { cancelled = true; };
  }, [base]);

  useEffect(() => {
    setApprovalMode(normalizeApprovalMode(localStorage.getItem(approvalModeStorageKey(agentId))));
    setCollaborationMode(localStorage.getItem(`agentkit:chat:collaboration:${agentId}`) === "plan" ? "plan" : "default");
    setReasoningEffort("");
  }, [agentId]);

  useEffect(() => {
    setCommandIndex(0);
  }, [input]);

  const selectedCloudModel = models.find(item => item.id === selectedModel);
  const effectiveReasoningEffort = explicitReasoningEfforts(selectedCloudModel).includes(reasoningEffort)
    ? reasoningEffort
    : "";
  useEffect(() => {
    if (reasoningEffort && !explicitReasoningEfforts(selectedCloudModel).includes(reasoningEffort)) {
      setReasoningEffort("");
    }
  }, [reasoningEffort, selectedCloudModel]);

  useEffect(() => {
    fallbackMessagesRef.current = [];
    durableEventsRef.current = [];
    refreshMessages(currentSessionId).catch(error => {
      showToast("云端消息加载失败", error.message, "error");
    });
    refreshInteractions(currentSessionId).catch(error => {
      showToast("云端交互加载失败", error.message, "error");
    });
  }, [currentSessionId, refreshInteractions, refreshMessages]);

  useEffect(() => {
    const list = messageListRef.current;
    if (list && followTailRef.current) list.scrollTop = list.scrollHeight;
  }, [messages, sending, streamingRuntimeItems, waitingForResponse]);

  async function createSession(): Promise<string> {
    const response = await apiFetch(`${base}/sessions`, { method: "POST" });
    if (!response.ok) throw new Error(await responseError(response));
    const payload = await response.json();
    const raw = payload.session ?? payload.Session ?? payload;
    const session = normalizeSession(raw);
    if (!session) throw new Error("云端未返回有效会话标识");
    setSessions(previous => [session, ...previous.filter(item => item.id !== session.id)]);
    currentSessionIdRef.current = session.id;
    setCurrentSessionId(session.id);
    setMessages([]);
    fallbackMessagesRef.current = [];
    durableEventsRef.current = [];
    setStreamingRuntimeItems([]);
    streamedFramesRef.current = [];
    setInteractions([]);
    setRunError("");
    followTailRef.current = true;
    assistantIdsBeforeSendRef.current = new Set();
    sessionCursorRef.current.set(session.id, 0);
    return session.id;
  }

  async function startNewSession() {
    if (sending || waitingForResponse) return;
    try {
      await createSession();
      setSessionPanelOpen(false);
    } catch (error) {
      showToast("新建云端会话失败", error instanceof Error ? error.message : String(error), "error");
    }
  }

  async function sendMessage() {
    const submission = parseComposerSubmission(input);
    if (submission.kind === "toggle-plan") {
      selectComposerCommand("plan");
      return;
    }
    if (submission.kind === "set-default") {
      selectComposerCommand("default");
      return;
    }
    if (submission.kind === "goal" && !submission.objective) {
      showToast("请补充目标", "在 /goal 后输入需要持续完成的目标", "error");
      return;
    }
    const goalObjective = submission.kind === "goal" ? submission.objective : "";
    const content = submission.kind === "message" ? submission.text : goalObjective;
    // React state is committed after the handler returns.  The ref closes the
    // small gap in which Enter and a click could both create an initial cloud
    // session before `sending` has rendered as true.
    if ((!content && attachments.length === 0) || sending || waitingForResponse || sendInFlightRef.current) return;
    // The optimistic message is the durable visual record of what was sent.
    // Clear the draft immediately and keep failures in the timeline instead
    // of silently putting stale input back into the composer.
    setInput("");
    followTailRef.current = true;
    sendInFlightRef.current = true;
    setSending(true);
    waitingForResponseRef.current = true;
    setWaitingForResponse(true);
    setStreamingRuntimeItems([]);
    streamedFramesRef.current = [];
    setRunError("");
    try {
      const contentParts: Array<Record<string, unknown>> = [];
      if (content) contentParts.push({ type: "input_text", text: content });
      for (const file of attachments) {
        const dataUrl = await fileDataUrl(file);
        contentParts.push(file.type.startsWith("image/")
          ? { type: "input_image", image_url: dataUrl }
          : { type: "input_file", filename: file.name, file_data: dataUrl });
      }
      const sessionId = currentSessionIdRef.current || await createSession();
      assistantIdsBeforeSendRef.current = new Set(
        messages
          .filter(message => !message.pending && message.role === "assistant")
          .map(message => message.id),
      );
      awaitingRunIdRef.current = "";
      // Do not invent a RunAgent field for client-side correlation.  The
      // deployed Server contract owns run/invocation ids; correlate this turn
      // by the durable SessionEvent cursor, then pin to the first new cloud id.
      awaitingInvocationIdRef.current = "";
      awaitingAcceptedSeqRef.current = sessionCursorRef.current.get(sessionId) || 0;
      const optimistic: CloudMessage = {
        id: `local-${crypto.randomUUID()}`,
        role: "user",
        content: content || `已上传 ${attachments.length} 个附件`,
        timestamp: new Date().toISOString(),
        pending: true,
      };
      setMessages(previous => [...previous, optimistic]);
      streamAbortRef.current?.abort();
      const streamController = new AbortController();
      streamAbortRef.current = streamController;
      directStreamActiveRef.current = true;
      projectedStreamEventIdsRef.current = new Set();
      const projectStreamItem = (item: CloudRuntimeItem, source: "direct" | "session") => {
        const sourcedItem = { ...item, source };
        setStreamingRuntimeItems(previous => {
          // Keep both transports in state. Rendering prefers canonical
          // SessionEvent item identities and exposes only the not-yet-caught-
          // up suffix from the faster identity-less direct stream.
          return mergeRuntimeItem(previous, sourcedItem);
        });
      };
      const streamUrl = `${base}/sessions/${encodeURIComponent(sessionId)}/events/stream?afterSeqId=${awaitingAcceptedSeqRef.current}`;
      apiFetch(streamUrl, {
        headers: { Accept: "text/event-stream" },
        signal: streamController.signal,
      }).then(response => consumeSseResponse(response, frame => {
        const envelope = runtimeEnvelope(frame);
        if (!envelope) return;
        if (envelope.seq) {
          sessionCursorRef.current.set(
            sessionId,
            Math.max(sessionCursorRef.current.get(sessionId) || 0, envelope.seq),
          );
        }
        const eventIds = [envelope.runId, envelope.invocationId].filter(Boolean);
        const expectedRunIds = [awaitingRunIdRef.current, awaitingInvocationIdRef.current].filter(Boolean);
        if (expectedRunIds.length && (!eventIds.length || !eventIds.some(id => expectedRunIds.includes(id)))) return;
        if (awaitingAcceptedSeqRef.current && envelope.seq && envelope.seq <= awaitingAcceptedSeqRef.current) return;
        if (!expectedRunIds.length) {
          if (envelope.runId) awaitingRunIdRef.current = envelope.runId;
          if (envelope.invocationId) awaitingInvocationIdRef.current = envelope.invocationId;
        }
        streamedFramesRef.current = [...streamedFramesRef.current, frame].slice(-500);
        setInteractions(pendingInteractions(streamedFramesRef.current));
        const item = runtimeItemPatch(frame);
        if (item) {
          const eventIdentity = streamEventIdentity(frame);
          const alreadyProjected = Boolean(eventIdentity && projectedStreamEventIdsRef.current.has(eventIdentity));
          if (eventIdentity) projectedStreamEventIdsRef.current.add(eventIdentity);
          if (!alreadyProjected) projectStreamItem(item, "session");
        }
        const terminal = terminalRunEvent(
          [frame],
          awaitingRunIdRef.current,
          awaitingInvocationIdRef.current,
          awaitingAcceptedSeqRef.current,
        );
        if (terminal && terminal.status !== "completed") {
          settleCloudRun(terminal.status === "failed"
            ? terminal.error || "本次请求已结束，未得到回复。"
            : "");
          refreshMessages(sessionId).catch(() => {});
          refreshInteractions(sessionId).catch(() => {});
          refreshSessions().catch(() => {});
        }
      }, streamController.signal)).catch(() => {
        // Timed polling remains the compatibility fallback if the canonical
        // event stream is unavailable, but the stream is opened before the
        // blocking RunAgent response so real deltas can render immediately.
      });
      const response = await apiFetch(`${base}/sessions/${encodeURIComponent(sessionId)}/messages/stream`, {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
        signal: streamController.signal,
        body: JSON.stringify({
          content: contentParts,
          model: selectedModel || undefined,
          modelOptions: effectiveReasoningEffort ? { reasoning: { effort: effectiveReasoningEffort } } : {},
          toolApprovalMode: approvalMode,
          collaborationMode,
          goalObjective: goalObjective || undefined,
        }),
      });
      let foregroundTerminal: TerminalRunResult | null = null;
      await consumeSseResponse(response, frame => {
        streamedFramesRef.current = [...streamedFramesRef.current, frame].slice(-500);
        setInteractions(pendingInteractions(streamedFramesRef.current));
        const eventIdentity = streamEventIdentity(frame);
        const alreadyProjected = Boolean(eventIdentity && projectedStreamEventIdsRef.current.has(eventIdentity));
        if (eventIdentity) projectedStreamEventIdsRef.current.add(eventIdentity);
        if (!alreadyProjected) {
          directStreamItemPatches(frame).forEach(item => projectStreamItem(item, "direct"));
        }
        const terminal = directStreamTerminal(frame);
        if (terminal) {
          foregroundTerminal = terminal;
          if (terminal.status === "failed") {
            settleCloudRun(terminal.error, "云端流式响应失败");
          }
        }
      }, streamController.signal);
      // Do not expose the terminal idle state until foreground deltas have
      // converged with the durable SessionEvent projection. Otherwise React
      // briefly renders the same assistant item once from each source and the
      // user sees a duplicate flash immediately before history settles.
      if (waitingForResponseRef.current) {
        try {
          await refreshInteractions(sessionId);
          await refreshMessages(sessionId);
        } catch {
          // The foreground answer remains usable when an older deployment has
          // no durable history endpoints; the next explicit reload can retry.
        }
        settleCloudRun(
          foregroundTerminal?.status === "failed" ? foregroundTerminal.error : "",
          "云端流式响应失败",
        );
      }
      setAttachments([]);
      refreshSessions().catch(() => {});
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      if (waitingForResponseRef.current) settleCloudRun(message, "云端消息发送失败");
    } finally {
      setSending(false);
      sendInFlightRef.current = false;
    }
  }

  function setCloudCollaborationMode(next: CollaborationMode) {
    setCollaborationMode(next);
    localStorage.setItem(`agentkit:chat:collaboration:${agentId}`, next);
    setInput("");
    showToast(next === "plan" ? "计划模式已开启" : "已返回默认模式", "下一轮云端对话生效", "success");
  }

  function selectComposerCommand(id: ComposerCommand["id"]) {
    if (id === "goal") {
      setInput("/goal ");
      return;
    }
    setCloudCollaborationMode(id === "plan" ? (collaborationMode === "plan" ? "default" : "plan") : "default");
  }

  function retryLastMessage() {
    const latestUserMessage = [...messages].reverse().find(message => message.role === "user");
    if (!latestUserMessage) return;
    setInput(latestUserMessage.content);
    setRunError("");
  }

  async function deleteSession(sessionId: string) {
    if (deleting || !window.confirm("确定删除这个云端会话吗？")) return;
    setDeleting(sessionId);
    try {
      const response = await apiFetch(`${base}/sessions/${encodeURIComponent(sessionId)}`, { method: "DELETE" });
      if (!response.ok) throw new Error(await responseError(response));
      setSessions(previous => previous.filter(item => item.id !== sessionId));
      const deletedCurrent = currentSessionIdRef.current === sessionId;
      if (deletedCurrent) {
        streamAbortRef.current?.abort();
        streamAbortRef.current = null;
        currentSessionIdRef.current = "";
        setCurrentSessionId("");
        setMessages([]);
        fallbackMessagesRef.current = [];
        durableEventsRef.current = [];
        setStreamingRuntimeItems([]);
        streamedFramesRef.current = [];
        setInteractions([]);
        setRunError("");
      }
      sessionCursorRef.current.delete(sessionId);
      await refreshSessions(!deletedCurrent);
    } catch (error) {
      showToast("删除云端会话失败", error instanceof Error ? error.message : String(error), "error");
    } finally {
      setDeleting("");
    }
  }

  async function submitInteraction(interaction: CloudInteraction, action: "approve" | "reject" | "submit") {
    if (!currentSessionId || resolvingInteractionId) return;
    if (!interaction.runId) {
      showToast("交互缺少运行标识", "请刷新会话后重试。", "error");
      return;
    }
    setResolvingInteractionId(interaction.id);
    try {
      const response = await apiFetch(`${base}/sessions/${encodeURIComponent(currentSessionId)}/interactions`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          runId: interaction.runId,
          interactionId: interaction.id,
          expectedRevision: interaction.revision,
          action,
          response: action === "approve" ? { decision: "approve" } : action === "reject" ? { decision: "reject" } : {},
          idempotencyKey: `studio-cloud-${interaction.id}-${interaction.revision}-${action}`,
        }),
      });
      if (!response.ok) throw new Error(await responseError(response));
      await Promise.all([refreshInteractions(currentSessionId), refreshMessages(currentSessionId)]);
      showToast("已提交确认", "云端 Agent 将继续当前对话。", "success");
    } catch (error) {
      showToast("提交确认失败", error instanceof Error ? error.message : String(error), "error");
    } finally {
      setResolvingInteractionId("");
    }
  }

  return (
    <section className={`studio-chat-shell cloud-chat-shell${sessionPanelOpen ? " sessions-open" : ""}`} aria-label="云端会话">
      <aside className="chat-session-sidebar" aria-label="云端会话历史">
        <header className="chat-session-header">
          <div><h2>云端会话</h2><span>{agentName}</span></div>
          <div className="chat-session-header-actions">
            <button className="icon-button tertiary" type="button" onClick={startNewSession} disabled={sending || waitingForResponse} aria-label="新建云端会话" title="新建云端会话"><MessageSquarePlus size={17} /></button>
            <button className="icon-button tertiary chat-session-mobile-close" type="button" aria-label="关闭云端会话历史" title="关闭云端会话历史" onClick={() => setSessionPanelOpen(false)}><X size={17} /></button>
          </div>
        </header>
        <div className="chat-session-list" role="list">
          {loading && (
            <div className="chat-session-skeleton" role="status" aria-label="正在同步云端会话">
              <i /><i /><i />
            </div>
          )}
          {!loading && !sessions.length && <p className="chat-sidebar-empty">还没有云端会话</p>}
          {sessions.map(session => {
            const activity = cloudSessionActivity(session.state);
            return (
            <div className={`chat-session-item${session.id === currentSessionId ? " active" : ""}${activity === "running" ? " running" : ""}`} key={session.id} role="listitem">
              <button className="chat-session-main" type="button" onClick={() => {
                currentSessionIdRef.current = session.id;
                setCurrentSessionId(session.id);
                setRunError(session.error);
                setSessionPanelOpen(false);
              }}>
                <strong>{session.title}</strong>
                {activity && (
                  <span
                    className={`session-status ${activity}`}
                    aria-label={activity === "running" ? "运行中" : activity === "waiting_input" ? "等待输入" : "运行失败"}
                  />
                )}
              </button>
              <button className="chat-session-delete" type="button" aria-label={`删除会话 ${session.title}`} title="删除会话" disabled={deleting === session.id} onClick={() => deleteSession(session.id)}><Trash2 size={15} /></button>
            </div>
          )})}
        </div>
      </aside>
      <button className="chat-session-backdrop" type="button" aria-label="关闭云端会话历史" onClick={() => setSessionPanelOpen(false)} />
      <div className="chat-conversation">
        <header className="chat-conversation-header">
          <button
            className="icon-button tertiary chat-session-mobile-trigger"
            type="button"
            aria-label="打开云端会话历史"
            title="云端会话历史"
            aria-expanded={sessionPanelOpen}
            onClick={() => setSessionPanelOpen(true)}
          >
            <PanelLeftOpen size={17} />
          </button>
          <div><h1>{agentName}</h1><span>云端 Agent · {agentId}</span></div>
        </header>
        <div
          ref={messageListRef}
          className="chat-message-list"
          role="log"
          aria-live="polite"
          aria-busy={sending || waitingForResponse}
          onScroll={event => {
            const target = event.currentTarget;
            followTailRef.current = target.scrollHeight - target.scrollTop - target.clientHeight < 64;
          }}
        >
          {!currentSessionId && !loading && <div className="chat-empty"><span className="chat-empty-icon"><Bot /></span><h2>开始一段云端会话</h2></div>}
          {(runError || cloudSessionActivity(sessions.find(session => session.id === currentSessionId)?.state || "") === "failed") && (
            <div className="cloud-chat-run-warning">
              <ShieldAlert size={15} />
              <span>{runError || sessions.find(session => session.id === currentSessionId)?.error || "这次云端运行未完成；可新建会话后重试。若持续失败，请到可观测页面按会话查看记录。"}</span>
              {runError && (
                <button className="text-button" type="button" aria-label="重试这条消息" onClick={retryLastMessage}>
                  重试
                </button>
              )}
            </div>
          )}
          {messages.map(message => (
            <article key={message.id} className={`message ${message.role}${message.pending ? " pending" : ""}${message.streaming ? " streaming" : ""}`}>
              <div className="message-meta">{message.role === "user" ? "你" : agentName}</div>
              <CloudMessageBody message={message} />
            </article>
          ))}
          <CloudRuntimeTimeline items={streamingRuntimeItems} agentName={agentName} streaming={waitingForResponse} />
          {(sending || waitingForResponse) && streamingRuntimeItems.length === 0 && <div className="cloud-chat-pending"><span className="text-shimmer">正在等待云端响应…</span></div>}
        </div>
        <div className="chat-composer-wrap">
          {interactions.length > 0 && (
            <div className="chat-pending-interactions" role="region" aria-label="待处理确认" data-ui="interaction-tray">
              <div className="chat-pending-interactions-heading"><ShieldAlert size={16} /><strong>等待你的确认</strong><span>处理后将继续当前云端对话</span></div>
              {interactions.map(interaction => (
                <div className="cloud-interaction-card" key={interaction.id}>
                  <div><strong>{interaction.kind === "approval" ? "工具操作需要批准" : interaction.title}</strong><span>{interaction.title}</span></div>
                  <div className="cloud-interaction-actions">
                    {interaction.kind === "approval" && <button className="secondary-button" type="button" disabled={Boolean(resolvingInteractionId)} onClick={() => submitInteraction(interaction, "reject")}><X size={15} />拒绝</button>}
                    <button className="primary-button" type="button" disabled={Boolean(resolvingInteractionId)} onClick={() => submitInteraction(interaction, interaction.kind === "approval" ? "approve" : "submit")}><ShieldCheck size={15} />{interaction.kind === "approval" ? "允许执行" : "提交"}</button>
                  </div>
                </div>
              ))}
            </div>
          )}
          <ChatComposer
            input={input}
            placeholder={collaborationMode === "plan" ? "描述需要云端 Agent 规划的任务…" : "发送到云端 Agent"}
            disabled={!active || sending || waitingForResponse}
            active={active}
            attachments={attachments.map((file, index) => ({
              id: `${index}:${file.name}:${file.size}`,
              name: file.name,
              kind: file.type.startsWith("image/") ? "image" : file.type.startsWith("text/") ? "text" : "file",
              size: file.size,
            }))}
            mode={collaborationMode}
            approvalMode={approvalMode}
            models={models.map((item): ComposerModelOption => ({
              id: item.id,
              label: item.label,
              reasoningEfforts: explicitReasoningEfforts(item),
            }))}
            model={selectedModel}
            reasoningEffort={reasoningEffort}
            commandIndex={commandIndex}
            canSend={Boolean(input.trim() || attachments.length)}
            attachmentAccept=""
            attachmentLimit={8}
            onInputChange={setInput}
            onFiles={files => {
              const oversized = files.find(file => file.size > 10 * 1024 * 1024);
              if (oversized) showToast("附件过大", `${oversized.name} 超过 10 MB 限制`, "error");
              setAttachments(previous => {
                const accepted = files.filter(file => file.size <= 10 * 1024 * 1024);
                if (previous.length + accepted.length > 8) showToast("附件过多", "每轮最多上传 8 个附件", "error");
                return [...previous, ...accepted].slice(0, 8);
              });
            }}
            onRemoveAttachment={id => {
              const targetIndex = Number(id.split(":", 1)[0]);
              setAttachments(previous => previous.filter((_, index) => index !== targetIndex));
            }}
            onSetMode={setCloudCollaborationMode}
            onStartGoal={() => selectComposerCommand("goal")}
            onApprovalModeChange={next => {
              setApprovalMode(next);
              localStorage.setItem(approvalModeStorageKey(agentId), next);
            }}
            onModelChange={setSelectedModel}
            onReasoningEffortChange={setReasoningEffort}
            onCommandSelect={selectComposerCommand}
            onCommandIndexChange={setCommandIndex}
            onSend={() => { void sendMessage(); }}
          />
          <p className="chat-composer-disclaimer">AI 生成内容可能不准确，请核对关键结论与工具操作。</p>
        </div>
      </div>
    </section>
  );
}

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkBreaks from "remark-breaks";
import remarkGfm from "remark-gfm";
import { Bot, BrainCircuit, Loader2, MessageSquarePlus, ShieldAlert, ShieldCheck, Trash2, Wrench, X } from "lucide-react";
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
  kind: "message" | "reasoning" | "tool" | "approval";
  title: string;
  text: string;
  detail: string;
  status: "running" | "waiting" | "completed" | "failed";
  operation: "append" | "replace";
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
  const nested = Object.keys(recordValue(content.runtime_event)).length
    ? recordValue(content.runtime_event)
    : recordValue(payload.runtime_event);
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

function runtimeItemPatch(value: unknown): CloudRuntimeItem | null {
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
    operation: scalarText(event.op).toLowerCase() === "append" ? "append" : "replace",
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

function directStreamTerminal(value: unknown): TerminalRunResult | null {
  const envelope = runtimeEnvelope(value);
  if (!envelope) return null;
  const event = envelope.event;
  const eventType = envelope.eventType;
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
  };
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

function CloudRuntimeProgress({ items, streaming }: { items: CloudRuntimeItem[]; streaming: boolean }) {
  const reasoning = items.filter(item => item.kind === "reasoning").map(item => item.text).join("");
  const activities = items.filter(item => item.kind === "tool" || item.kind === "approval");
  if (!reasoning && !activities.length) return null;
  const running = activities.find(item => item.status === "running" || item.status === "waiting");
  const title = running
    ? `${running.status === "waiting" ? "等待确认" : "正在处理"} · ${running.title}`
    : reasoning ? "正在思考" : `已处理 ${activities.length} 次工具调用`;
  return (
    <details className="chat-processing-group" open={streaming} data-ui="think">
      <summary>
        <BrainCircuit size={15} className="chat-processing-icon" />
        <span>{title}</span>
        {streaming && <Loader2 size={13} className="animate-spin" />}
      </summary>
      <div className="chat-processing-content">
        {reasoning && <div className="chat-reasoning-content">{reasoning}</div>}
        {activities.map(item => (
          <div className={`chat-activity-card ${item.kind}`} key={item.id}>
            <div className="chat-activity-row">
              <span className="chat-activity-icon">{item.kind === "approval" ? <ShieldAlert size={15} /> : <Wrench size={15} />}</span>
              <span className="chat-activity-copy"><small>{item.kind === "approval" ? "批准" : "工具"}</small><strong>{item.title}</strong></span>
              <span className={`chat-activity-status ${item.status}`}>
                {item.status === "completed" ? "已完成" : item.status === "failed" ? "失败" : item.status === "waiting" ? "等待确认" : "运行中"}
              </span>
            </div>
            {item.detail && <pre>{item.detail}</pre>}
          </div>
        ))}
      </div>
    </details>
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
  const messageListRef = useRef<HTMLDivElement>(null);
  const currentSessionIdRef = useRef("");
  const waitingForResponseRef = useRef(false);
  const assistantIdsBeforeSendRef = useRef<Set<string>>(new Set());
  const awaitingRunIdRef = useRef("");
  const awaitingInvocationIdRef = useRef("");
  const awaitingAcceptedSeqRef = useRef(0);
  const sendInFlightRef = useRef(false);
  const streamAbortRef = useRef<AbortController | null>(null);
  const streamedFramesRef = useRef<unknown[]>([]);
  const directStreamActiveRef = useRef(false);
  const directKindsSeenRef = useRef<Set<CloudRuntimeItem["kind"]>>(new Set());

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
    assistantIdsBeforeSendRef.current = new Set();
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
    const rows = (payload.messages || [])
      .map(normalizeMessage)
      .filter((item: CloudMessage | null): item is CloudMessage => Boolean(item));
    setMessages(rows);
    const hasNewAssistant = rows.some(
      message => message.role === "assistant" && !assistantIdsBeforeSendRef.current.has(message.id),
    );
    if (hasNewAssistant && !directStreamActiveRef.current) {
      setStreamingRuntimeItems([]);
      streamedFramesRef.current = [];
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
    const response = await apiFetch(`${base}/sessions/${encodeURIComponent(sessionId)}/events`);
    if (!response.ok) throw new Error(await responseError(response));
    const payload = await response.json();
    const events = payload.events || [];
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
  }, [base, settleCloudRun]);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setSessions([]);
    setCurrentSessionId("");
    currentSessionIdRef.current = "";
    setMessages([]);
    setStreamingRuntimeItems([]);
    streamedFramesRef.current = [];
    setInteractions([]);
    setRunError("");
    waitingForResponseRef.current = false;
    directStreamActiveRef.current = false;
    directKindsSeenRef.current = new Set();
    setWaitingForResponse(false);
    awaitingRunIdRef.current = "";
    awaitingInvocationIdRef.current = "";
    awaitingAcceptedSeqRef.current = 0;
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
    refreshMessages(currentSessionId).catch(error => {
      showToast("云端消息加载失败", error.message, "error");
    });
    refreshInteractions(currentSessionId).catch(error => {
      showToast("云端交互加载失败", error.message, "error");
    });
  }, [currentSessionId, refreshInteractions, refreshMessages]);

  useEffect(() => {
    if (!active || !currentSessionId) return;
    const timer = window.setInterval(() => {
      refreshMessages(currentSessionId).catch(() => {});
      refreshInteractions(currentSessionId).catch(() => {});
      refreshSessions().catch(() => {});
    }, sending || waitingForResponse ? 1200 : 4000);
    return () => window.clearInterval(timer);
  }, [active, currentSessionId, refreshInteractions, refreshMessages, refreshSessions, sending, waitingForResponse]);

  useEffect(() => {
    const list = messageListRef.current;
    if (list) list.scrollTop = list.scrollHeight;
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
    setStreamingRuntimeItems([]);
    streamedFramesRef.current = [];
    setInteractions([]);
    setRunError("");
    assistantIdsBeforeSendRef.current = new Set();
    return session.id;
  }

  async function startNewSession() {
    if (sending || waitingForResponse) return;
    try {
      await createSession();
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
      awaitingInvocationIdRef.current = "";
      const cursorResponse = await apiFetch(
        `${base}/sessions/${encodeURIComponent(sessionId)}/events`,
      );
      if (!cursorResponse.ok) throw new Error(await responseError(cursorResponse));
      const cursorPayload = await cursorResponse.json() as { events?: unknown[] };
      awaitingAcceptedSeqRef.current = (cursorPayload.events || []).reduce<number>((latest, event) => {
        if (!event || typeof event !== "object") return latest;
        const record = event as Record<string, unknown>;
        const payload = record.payload && typeof record.payload === "object"
          ? record.payload as Record<string, unknown>
          : record;
        const seq = Number(
          payload.seq ?? payload.seq_id ?? payload.source_session_seq
          ?? record.seq ?? record.seq_id ?? 0,
        ) || 0;
        return Math.max(latest, seq);
      }, 0);
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
      directKindsSeenRef.current = new Set();
      const projectStreamItem = (item: CloudRuntimeItem, source: "direct" | "session") => {
        if (source === "session") {
          // The foreground RunAgent stream owns assistant text. SessionEvent
          // remains the reconnect/history channel and a fallback for runtime
          // activity that the direct provider stream does not expose.
          if (item.kind === "message" || directKindsSeenRef.current.has(item.kind)) return;
          setStreamingRuntimeItems(previous => mergeRuntimeItem(previous, item));
          return;
        }
        const firstDirectItemOfKind = !directKindsSeenRef.current.has(item.kind);
        directKindsSeenRef.current.add(item.kind);
        setStreamingRuntimeItems(previous => mergeRuntimeItem(
          firstDirectItemOfKind ? previous.filter(existing => existing.kind !== item.kind) : previous,
          item,
        ));
      };
      const streamUrl = `${base}/sessions/${encodeURIComponent(sessionId)}/events/stream?afterSeqId=${awaitingAcceptedSeqRef.current}`;
      apiFetch(streamUrl, {
        headers: { Accept: "text/event-stream" },
        signal: streamController.signal,
      }).then(response => consumeSseResponse(response, frame => {
        const envelope = runtimeEnvelope(frame);
        if (!envelope) return;
        const eventIds = [envelope.runId, envelope.invocationId].filter(Boolean);
        const expectedRunIds = [awaitingRunIdRef.current, awaitingInvocationIdRef.current].filter(Boolean);
        if (expectedRunIds.length && eventIds.length && !eventIds.some(id => expectedRunIds.includes(id))) return;
        if (awaitingAcceptedSeqRef.current && envelope.seq && envelope.seq <= awaitingAcceptedSeqRef.current) return;
        if (!expectedRunIds.length) {
          if (envelope.runId) awaitingRunIdRef.current = envelope.runId;
          if (envelope.invocationId) awaitingInvocationIdRef.current = envelope.invocationId;
        }
        streamedFramesRef.current = [...streamedFramesRef.current, frame].slice(-500);
        setInteractions(pendingInteractions(streamedFramesRef.current));
        const item = runtimeItemPatch(frame);
        if (item) projectStreamItem(item, "session");
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
      await consumeSseResponse(response, frame => {
        streamedFramesRef.current = [...streamedFramesRef.current, frame].slice(-500);
        setInteractions(pendingInteractions(streamedFramesRef.current));
        directStreamItemPatches(frame).forEach(item => projectStreamItem(item, "direct"));
        const terminal = directStreamTerminal(frame);
        if (terminal) {
          settleCloudRun(
            terminal.status === "failed" ? terminal.error : "",
            "云端流式响应失败",
          );
        }
      }, streamController.signal);
      // A clean EOF is terminal even for providers that omit [DONE].
      if (waitingForResponseRef.current) settleCloudRun();
      setAttachments([]);
      refreshSessions().catch(() => {});
      refreshMessages(sessionId).catch(() => {});
      refreshInteractions(sessionId).catch(() => {});
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
        setStreamingRuntimeItems([]);
        streamedFramesRef.current = [];
        setInteractions([]);
        setRunError("");
      }
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

  const streamingAssistantText = streamingRuntimeItems
    .filter(item => item.kind === "message")
    .map(item => item.text)
    .join("");

  return (
    <section className="studio-chat-shell cloud-chat-shell" aria-label="云端会话">
      <aside className="chat-session-sidebar">
        <div className="chat-session-header">
          <div><strong>云端会话</strong><span>{agentName}</span></div>
          <button className="icon-button tertiary" type="button" onClick={startNewSession} disabled={sending || waitingForResponse} aria-label="新建云端会话" title="新建云端会话"><MessageSquarePlus size={17} /></button>
        </div>
        <div className="chat-session-list" role="list">
          {loading && <div className="chat-list-loading"><Loader2 size={16} /> 正在同步…</div>}
          {!loading && !sessions.length && <p className="chat-sidebar-empty">还没有云端会话</p>}
          {sessions.map(session => {
            const activity = cloudSessionActivity(session.state);
            return (
            <div className={`chat-session-item${session.id === currentSessionId ? " active" : ""}${activity === "running" ? " running" : ""}`} key={session.id} role="listitem">
              <button className="chat-session-main" type="button" onClick={() => {
                currentSessionIdRef.current = session.id;
                setCurrentSessionId(session.id);
                setRunError(session.error);
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
      <div className="chat-conversation">
        <header className="chat-conversation-header">
          <div><strong>{agentName}</strong><span>云端 Agent · {agentId}</span></div>
        </header>
        <div ref={messageListRef} className="chat-message-list" aria-live="polite">
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
              <div className="message-content"><ReactMarkdown remarkPlugins={[remarkGfm, remarkBreaks]}>{message.content || "…"}</ReactMarkdown></div>
            </article>
          ))}
          <CloudRuntimeProgress items={streamingRuntimeItems} streaming={waitingForResponse} />
          {streamingAssistantText && (
            <article className="message assistant streaming" aria-label="云端流式回复">
              <div className="message-meta">{agentName}</div>
              <div className="message-content"><ReactMarkdown remarkPlugins={[remarkGfm, remarkBreaks]}>{streamingAssistantText}</ReactMarkdown></div>
            </article>
          )}
          {(sending || waitingForResponse) && <div className="cloud-chat-pending"><Loader2 size={15} /> 正在等待云端响应…</div>}
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
        </div>
      </div>
    </section>
  );
}

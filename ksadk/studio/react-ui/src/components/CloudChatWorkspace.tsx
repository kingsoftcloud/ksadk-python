import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkBreaks from "remark-breaks";
import remarkGfm from "remark-gfm";
import { Bot, Loader2, MessageSquarePlus, ShieldAlert, ShieldCheck, Trash2, X } from "lucide-react";
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
    if (!event || typeof event !== "object") continue;
    const frame = event as Record<string, unknown>;
    const payload = frame.payload && typeof frame.payload === "object"
      ? frame.payload as Record<string, unknown>
      : frame;
    const eventType = String(frame.event_type ?? frame.eventType ?? payload.event_type ?? payload.eventType ?? "");
    const metadata = frame.Metadata && typeof frame.Metadata === "object"
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
    if (["interaction.requested", "approval_request"].includes(eventType)) {
      const request = payload.request && typeof payload.request === "object"
        ? payload.request as Record<string, unknown>
        : {};
      requested.set(interactionId, {
        id: interactionId,
        runId: String(
          payload.run_id
          ?? payload.runId
          ?? frame.run_id
          ?? frame.runId
          ?? frame.InvocationId
          ?? frame.invocation_id
          ?? "",
        ),
        revision: Number(payload.revision ?? 1) || 1,
        kind: String(payload.kind ?? request.kind ?? (eventType === "approval_request" ? "approval" : "input")),
        title: valueText(
          request.title
          ?? request.message
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

function terminalRunEvent(events: unknown[], runId: string, afterSeq: number): "completed" | "failed" | null {
  if (!runId && afterSeq <= 0) return null;
  for (const event of events) {
    if (!event || typeof event !== "object") continue;
    const frame = event as Record<string, unknown>;
    const payload = frame.payload && typeof frame.payload === "object"
      ? frame.payload as Record<string, unknown>
      : frame;
    // RuntimeEvent v2 uses run_id. Older Server event history uses
    // invocation_id together with a run_status envelope; accept both while
    // the Server history endpoint is being migrated.
    const eventRunId = String(
      payload.run_id ?? payload.runId ?? payload.invocation_id ?? payload.invocationId
      ?? frame.run_id ?? frame.runId ?? frame.invocation_id ?? frame.invocationId ?? "",
    );
    const eventSeq = Number(
      payload.seq ?? payload.seq_id ?? payload.source_session_seq
      ?? frame.seq ?? frame.seq_id ?? frame.source_session_seq ?? 0,
    ) || 0;
    // The current pre-production Server projection exposes the admitted
    // Runtime run id in the receipt but still labels historical events with
    // the outer invocation id.  Prefer an exact id match, then fall back to
    // the receipt's accepted Session sequence.  The composer admits one run
    // at a time, so the sequence window remains unambiguous for this client.
    const matchesRun = Boolean(runId) && eventRunId === runId;
    const matchesAcceptedWindow = afterSeq > 0 && eventSeq > afterSeq;
    if (!matchesRun && !matchesAcceptedWindow) continue;
    const eventType = String(frame.event_type ?? frame.eventType ?? payload.event_type ?? payload.eventType ?? "").toLowerCase();
    if (["run.completed", "run.complete", "run.succeeded"].includes(eventType)) return "completed";
    if (["run.failed", "run.cancelled", "run.expired", "run.error"].includes(eventType)) return "failed";
    if (["run_status", "run.status"].includes(eventType)) {
      const content = payload.content && typeof payload.content === "object"
        ? payload.content as Record<string, unknown>
        : {};
      const stateDelta = payload.state_delta && typeof payload.state_delta === "object"
        ? payload.state_delta as Record<string, unknown>
        : {};
      const activeRun = stateDelta.active_run && typeof stateDelta.active_run === "object"
        ? stateDelta.active_run as Record<string, unknown>
        : {};
      const status = String(payload.status ?? content.status ?? activeRun.status ?? "").toLowerCase();
      if (["completed", "complete", "succeeded", "success"].includes(status)) return "completed";
      if (["failed", "cancelled", "canceled", "expired", "error", "aborted"].includes(status)) return "failed";
    }
  }
  return null;
}

function streamedAssistantText(value: unknown): { text: string; cumulative: boolean } | null {
  if (!value || typeof value !== "object") return null;
  const frame = value as Record<string, unknown>;
  const payload = frame.payload && typeof frame.payload === "object"
    ? frame.payload as Record<string, unknown>
    : frame;
  const content = payload.content && typeof payload.content === "object"
    ? payload.content as Record<string, unknown>
    : {};
  const runtimeEvent = (content.runtime_event && typeof content.runtime_event === "object"
    ? content.runtime_event
    : payload.runtime_event && typeof payload.runtime_event === "object"
      ? payload.runtime_event
      : {}) as Record<string, unknown>;
  const update = (runtimeEvent.update && typeof runtimeEvent.update === "object"
    ? runtimeEvent.update
    : payload.update && typeof payload.update === "object"
      ? payload.update
      : {}) as Record<string, unknown>;
  const eventType = String(
    frame.event_type ?? frame.eventType ?? payload.event_type ?? payload.eventType
    ?? runtimeEvent.type ?? "",
  ).toLowerCase();
  if (["item.updated", "message.updated", "assistant_message.updated"].includes(eventType)) {
    const itemKind = String(runtimeEvent.item_kind ?? payload.item_kind ?? "").toLowerCase();
    if (itemKind && !["message", "assistant_message", "assistant"].includes(itemKind)) {
      return null;
    }
    const text = valueText(update.text ?? update.content ?? content.text ?? payload.text);
    const operation = String(runtimeEvent.op ?? payload.op ?? "").toLowerCase();
    return text ? { text, cumulative: operation !== "append" } : null;
  }
  if (["message.delta", "response.output_text.delta", "output_text.delta"].includes(eventType)) {
    const text = valueText(update.delta ?? update.text ?? payload.delta ?? content.delta ?? payload.text);
    return text ? { text, cumulative: false } : null;
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
  signal.addEventListener("abort", cancel, { once: true });
  try {
    while (!signal.aborted) {
      const { done, value } = await reader.read();
      buffer += decoder.decode(value, { stream: !done });
      const frames = buffer.split(/\r?\n\r?\n/);
      buffer = frames.pop() || "";
      for (const rawFrame of frames) {
        const data = rawFrame
          .split(/\r?\n/)
          .filter(line => line.startsWith("data:"))
          .map(line => line.slice(5).trimStart())
          .join("\n");
        if (!data || data === "[DONE]") continue;
        try {
          onFrame(JSON.parse(data));
        } catch {
          // Ignore a malformed frame and let the authoritative message poll
          // reconcile the conversation instead of terminating the stream.
        }
      }
      if (done) break;
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
  const [streamingAssistant, setStreamingAssistant] = useState<CloudMessage | null>(null);
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
  const [deleting, setDeleting] = useState("");
  const [resolvingInteractionId, setResolvingInteractionId] = useState("");
  const messageListRef = useRef<HTMLDivElement>(null);
  const currentSessionIdRef = useRef("");
  const waitingForResponseRef = useRef(false);
  const assistantIdsBeforeSendRef = useRef<Set<string>>(new Set());
  const awaitingRunIdRef = useRef("");
  const awaitingAcceptedSeqRef = useRef(0);
  const sendInFlightRef = useRef(false);
  const streamAbortRef = useRef<AbortController | null>(null);

  const base = useMemo(
    () => `/api/v1/deployments/${encodeURIComponent(deploymentId)}/cloud-chat`,
    [deploymentId],
  );

  const refreshSessions = useCallback(async (selectFallback = true) => {
    const response = await apiFetch(`${base}/sessions`);
    if (!response.ok) throw new Error(await responseError(response));
    const payload = await response.json() as { sessions?: unknown[]; items?: unknown[] };
    const rows = (payload.sessions || payload.items || [])
      .map(normalizeSession)
      .filter((item: CloudSession | null): item is CloudSession => Boolean(item));
    setSessions(rows);
    setCurrentSessionId(previous => {
      const next = rows.some(item => item.id === previous)
        ? previous
        : selectFallback ? rows[0]?.id || "" : "";
      currentSessionIdRef.current = next;
      return next;
    });
  }, [base]);

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
    if (hasNewAssistant) setStreamingAssistant(null);
    if (waitingForResponseRef.current && hasNewAssistant) {
      waitingForResponseRef.current = false;
      setWaitingForResponse(false);
      awaitingRunIdRef.current = "";
      awaitingAcceptedSeqRef.current = 0;
      assistantIdsBeforeSendRef.current = new Set();
      setStreamingAssistant(null);
    }
  }, [base]);

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
      awaitingAcceptedSeqRef.current,
    );
    if (terminal) {
      waitingForResponseRef.current = false;
      setWaitingForResponse(false);
      awaitingRunIdRef.current = "";
      awaitingAcceptedSeqRef.current = 0;
      if (terminal === "failed") {
        showToast("云端运行未完成", "本次请求已结束，未得到回复。可新建会话后重试；若持续失败，请到可观测页面按会话查看记录。", "error");
      }
    }
    setInteractions(pendingInteractions(events));
  }, [base]);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setSessions([]);
    setCurrentSessionId("");
    currentSessionIdRef.current = "";
    setMessages([]);
    setStreamingAssistant(null);
    setInteractions([]);
    waitingForResponseRef.current = false;
    setWaitingForResponse(false);
    awaitingRunIdRef.current = "";
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
  }, [messages, sending, waitingForResponse]);

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
    setStreamingAssistant(null);
    setInteractions([]);
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
    sendInFlightRef.current = true;
    setSending(true);
    waitingForResponseRef.current = true;
    setWaitingForResponse(true);
    setStreamingAssistant(null);
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
      const cursorResponse = await apiFetch(
        `${base}/sessions/${encodeURIComponent(sessionId)}/events`,
      );
      if (!cursorResponse.ok) throw new Error(await responseError(cursorResponse));
      const cursorPayload = await cursorResponse.json() as { events?: unknown[] };
      awaitingAcceptedSeqRef.current = (cursorPayload.events || []).reduce((latest, event) => {
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
      const streamUrl = `${base}/sessions/${encodeURIComponent(sessionId)}/events/stream?afterSeqId=${awaitingAcceptedSeqRef.current}`;
      apiFetch(streamUrl, {
        headers: { Accept: "text/event-stream" },
        signal: streamController.signal,
      }).then(response => consumeSseResponse(response, frame => {
        if (!frame || typeof frame !== "object") return;
        const record = frame as Record<string, unknown>;
        const payload = record.payload && typeof record.payload === "object"
          ? record.payload as Record<string, unknown>
          : record;
        const eventRunId = String(
          payload.run_id ?? payload.runId ?? record.run_id ?? record.runId ?? "",
        );
        const eventSeq = Number(payload.seq ?? payload.seq_id ?? record.seq ?? record.seq_id ?? 0) || 0;
        if (awaitingRunIdRef.current && eventRunId && eventRunId !== awaitingRunIdRef.current) return;
        if (awaitingAcceptedSeqRef.current && eventSeq && eventSeq <= awaitingAcceptedSeqRef.current) return;
        const delta = streamedAssistantText(frame);
        if (delta) {
          setStreamingAssistant(previous => ({
            id: `stream-${awaitingRunIdRef.current || sessionId}`,
            role: "assistant",
            content: delta.cumulative ? delta.text : `${previous?.content || ""}${delta.text}`,
            timestamp: new Date().toISOString(),
            streaming: true,
          }));
        }
        const terminal = terminalRunEvent(
          [frame],
          awaitingRunIdRef.current,
          awaitingAcceptedSeqRef.current,
        );
        if (terminal) {
          waitingForResponseRef.current = false;
          setWaitingForResponse(false);
          refreshMessages(sessionId).catch(() => {});
          refreshInteractions(sessionId).catch(() => {});
          refreshSessions().catch(() => {});
        }
      }, streamController.signal)).catch(() => {
        // Timed polling remains the compatibility fallback if the canonical
        // event stream is unavailable, but the stream is opened before the
        // blocking RunAgent response so real deltas can render immediately.
      });
      const response = await apiFetch(`${base}/sessions/${encodeURIComponent(sessionId)}/messages`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          content: contentParts,
          model: selectedModel || undefined,
          modelOptions: effectiveReasoningEffort ? { reasoning: { effort: effectiveReasoningEffort } } : {},
          toolApprovalMode: approvalMode,
          collaborationMode,
          goalObjective: goalObjective || undefined,
        }),
      });
      if (!response.ok) throw new Error(await responseError(response));
      setInput("");
      setAttachments([]);
      const receipt = await response.json() as Record<string, unknown>;
      awaitingRunIdRef.current = String(receipt.run_id ?? receipt.runId ?? receipt.RunId ?? "");
      awaitingAcceptedSeqRef.current = Math.max(
        awaitingAcceptedSeqRef.current,
        Number(receipt.accepted_seq ?? receipt.acceptedSeq ?? receipt.AcceptedSeq ?? 0) || 0,
      );
      // Admission is complete once the receipt arrives. Session-list refresh
      // is metadata work and must not keep the composer in `sending` while
      // the runtime response is already available.
      refreshSessions().catch(() => {});
      window.setTimeout(() => {
        refreshMessages(sessionId).catch(() => {});
        refreshInteractions(sessionId).catch(() => {});
      }, 250);
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      setMessages(previous => previous.map(item => item.pending ? { ...item, pending: false } : item));
      waitingForResponseRef.current = false;
      setWaitingForResponse(false);
      awaitingRunIdRef.current = "";
      awaitingAcceptedSeqRef.current = 0;
      assistantIdsBeforeSendRef.current = new Set();
      streamAbortRef.current?.abort();
      streamAbortRef.current = null;
      showToast("云端消息发送失败", message, "error");
    } finally {
      setSending(false);
      sendInFlightRef.current = false;
    }
  }

  function setCloudCollaborationMode(next: CollaborationMode) {
    setCollaborationMode(next);
    localStorage.setItem(`agentkit:chat:collaboration:${agentId}`, next);
    setInput("");
    showToast(next === "plan" ? "计划模式已开启" : "已切换到 Agent Loop", "下一轮云端对话生效", "success");
  }

  function selectComposerCommand(id: ComposerCommand["id"]) {
    if (id === "goal") {
      setInput("/goal ");
      return;
    }
    setCloudCollaborationMode(id === "plan" ? (collaborationMode === "plan" ? "default" : "plan") : "default");
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
        setStreamingAssistant(null);
        setInteractions([]);
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
          {sessions.find(session => session.id === currentSessionId)?.state === "failed" && <div className="cloud-chat-run-warning"><ShieldAlert size={15} />这次云端运行未完成；可新建会话后重试。若持续失败，请到可观测页面按会话查看记录。</div>}
          {messages.map(message => (
            <article key={message.id} className={`message ${message.role}${message.pending ? " pending" : ""}${message.streaming ? " streaming" : ""}`}>
              <div className="message-meta">{message.role === "user" ? "你" : agentName}</div>
              <div className="message-content"><ReactMarkdown remarkPlugins={[remarkGfm, remarkBreaks]}>{message.content || "…"}</ReactMarkdown></div>
            </article>
          ))}
          {streamingAssistant && !messages.some(message => message.id === streamingAssistant.id) && (
            <article key={streamingAssistant.id} className="message assistant streaming" aria-label="云端流式回复">
              <div className="message-meta">{agentName}</div>
              <div className="message-content"><ReactMarkdown remarkPlugins={[remarkGfm, remarkBreaks]}>{streamingAssistant.content}</ReactMarkdown></div>
            </article>
          )}
          {(sending || waitingForResponse) && <div className="cloud-chat-pending"><Loader2 size={15} /> 正在等待云端响应…</div>}
        </div>
        <div className="chat-composer-wrap">
          {interactions.length > 0 && (
            <div className="chat-pending-interactions" aria-label="待处理确认" data-ui="interaction-tray">
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

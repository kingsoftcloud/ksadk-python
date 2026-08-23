import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { Bot, Loader2, MessageSquarePlus, Send, ShieldAlert, ShieldCheck, Trash2, X } from "lucide-react";
import { apiFetch } from "../api";
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
}

interface CloudInteraction {
  id: string;
  runId: string;
  revision: number;
  kind: string;
  title: string;
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
    const interactionId = String(payload.interaction_id ?? payload.interactionId ?? "").trim();
    if (!interactionId) continue;
    if (eventType === "interaction.requested") {
      const request = payload.request && typeof payload.request === "object"
        ? payload.request as Record<string, unknown>
        : {};
      requested.set(interactionId, {
        id: interactionId,
        runId: String(payload.run_id ?? payload.runId ?? frame.run_id ?? frame.runId ?? ""),
        revision: Number(payload.revision ?? 1) || 1,
        kind: String(payload.kind ?? request.kind ?? "input"),
        title: valueText(request.title ?? request.message ?? request.kind ?? "需要你的确认") || "需要你的确认",
      });
    } else if (["interaction.resolved", "interaction.cancelled", "interaction.expired"].includes(eventType)) {
      requested.delete(interactionId);
    }
  }
  return [...requested.values()];
}

function terminalRunEvent(events: unknown[], runId: string): "completed" | "failed" | null {
  if (!runId) return null;
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
    if (eventRunId !== runId) continue;
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

async function responseError(response: Response): Promise<string> {
  try {
    const body = await response.json();
    return String(body?.error?.message || body?.message || body?.detail || `请求失败 (${response.status})`);
  } catch {
    return `请求失败 (${response.status})`;
  }
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
  const [interactions, setInteractions] = useState<CloudInteraction[]>([]);
  const [input, setInput] = useState("");
  const [loading, setLoading] = useState(true);
  const [sending, setSending] = useState(false);
  const [waitingForResponse, setWaitingForResponse] = useState(false);
  const [deleting, setDeleting] = useState("");
  const [resolvingInteractionId, setResolvingInteractionId] = useState("");
  const messageListRef = useRef<HTMLDivElement>(null);
  const currentSessionIdRef = useRef("");
  const waitingForResponseRef = useRef(false);
  const assistantCountBeforeSendRef = useRef(0);
  const awaitingRunIdRef = useRef("");
  const sendInFlightRef = useRef(false);

  const base = useMemo(
    () => `/api/v1/deployments/${encodeURIComponent(deploymentId)}/cloud-chat`,
    [deploymentId],
  );

  const refreshSessions = useCallback(async () => {
    const response = await apiFetch(`${base}/sessions`);
    if (!response.ok) throw new Error(await responseError(response));
    const payload = await response.json() as { sessions?: unknown[]; items?: unknown[] };
    const rows = (payload.sessions || payload.items || [])
      .map(normalizeSession)
      .filter((item: CloudSession | null): item is CloudSession => Boolean(item));
    setSessions(rows);
    setCurrentSessionId(previous => {
      const next = rows.some(item => item.id === previous) ? previous : rows[0]?.id || "";
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
    if (
      waitingForResponseRef.current
      && rows.filter(message => message.role === "assistant").length > assistantCountBeforeSendRef.current
    ) {
      waitingForResponseRef.current = false;
      setWaitingForResponse(false);
      awaitingRunIdRef.current = "";
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
    const terminal = terminalRunEvent(events, awaitingRunIdRef.current);
    if (terminal) {
      waitingForResponseRef.current = false;
      setWaitingForResponse(false);
      awaitingRunIdRef.current = "";
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
    setInteractions([]);
    waitingForResponseRef.current = false;
    setWaitingForResponse(false);
    awaitingRunIdRef.current = "";
    refreshSessions()
      .catch(error => { if (!cancelled) showToast("云端会话加载失败", error.message, "error"); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [refreshSessions, refreshTick]);

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
    setInteractions([]);
    assistantCountBeforeSendRef.current = 0;
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
    const content = input.trim();
    // React state is committed after the handler returns.  The ref closes the
    // small gap in which Enter and a click could both create an initial cloud
    // session before `sending` has rendered as true.
    if (!content || sending || waitingForResponse || sendInFlightRef.current) return;
    sendInFlightRef.current = true;
    setSending(true);
    waitingForResponseRef.current = true;
    setWaitingForResponse(true);
    setInput("");
    try {
      const sessionId = currentSessionIdRef.current || await createSession();
      assistantCountBeforeSendRef.current = messages.filter(
        message => !message.pending && message.role === "assistant",
      ).length;
      awaitingRunIdRef.current = "";
      const optimistic: CloudMessage = {
        id: `local-${crypto.randomUUID()}`,
        role: "user",
        content,
        timestamp: new Date().toISOString(),
        pending: true,
      };
      setMessages(previous => [...previous, optimistic]);
      const response = await apiFetch(`${base}/sessions/${encodeURIComponent(sessionId)}/messages`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ content }),
      });
      if (!response.ok) throw new Error(await responseError(response));
      const receipt = await response.json() as Record<string, unknown>;
      awaitingRunIdRef.current = String(receipt.run_id ?? receipt.runId ?? receipt.RunId ?? "");
      await refreshSessions();
      window.setTimeout(() => { refreshMessages(sessionId).catch(() => {}); }, 250);
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      setMessages(previous => previous.map(item => item.pending ? { ...item, pending: false } : item));
      waitingForResponseRef.current = false;
      setWaitingForResponse(false);
      awaitingRunIdRef.current = "";
      showToast("云端消息发送失败", message, "error");
    } finally {
      setSending(false);
      sendInFlightRef.current = false;
    }
  }

  async function deleteSession(sessionId: string) {
    if (deleting || !window.confirm("确定删除这个云端会话吗？")) return;
    setDeleting(sessionId);
    try {
      const response = await apiFetch(`${base}/sessions/${encodeURIComponent(sessionId)}`, { method: "DELETE" });
      if (!response.ok) throw new Error(await responseError(response));
      setSessions(previous => previous.filter(item => item.id !== sessionId));
      if (currentSessionId === sessionId) {
        currentSessionIdRef.current = "";
        setCurrentSessionId("");
        setMessages([]);
      }
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
          {sessions.map(session => (
            <div className={`chat-session-item${session.id === currentSessionId ? " active" : ""}`} key={session.id} role="listitem">
              <button className="chat-session-main" type="button" onClick={() => {
                currentSessionIdRef.current = session.id;
                setCurrentSessionId(session.id);
              }}>
                <strong>{session.title}</strong>
                <span>{session.state || session.updatedAt || "云端"}</span>
              </button>
              <button className="chat-session-delete" type="button" aria-label={`删除会话 ${session.title}`} title="删除会话" disabled={deleting === session.id} onClick={() => deleteSession(session.id)}><Trash2 size={15} /></button>
            </div>
          ))}
        </div>
      </aside>
      <div className="chat-conversation">
        <header className="chat-conversation-header">
          <div><strong>{agentName}</strong><span>云端 Agent · {agentId}</span></div>
        </header>
        <div ref={messageListRef} className="chat-message-list" aria-live="polite">
          {!currentSessionId && !loading && <div className="chat-empty"><span className="chat-empty-icon"><Bot /></span><h2>开始一段云端会话</h2><p>消息会由本地 Studio 通过受权的云端控制面发送。</p></div>}
          {sessions.find(session => session.id === currentSessionId)?.state === "failed" && <div className="cloud-chat-run-warning"><ShieldAlert size={15} />这次云端运行未完成；可新建会话后重试。若持续失败，请到可观测页面按会话查看记录。</div>}
          {messages.map(message => (
            <article key={message.id} className={`message ${message.role}${message.pending ? " pending" : ""}`}>
              <div className="message-meta">{message.role === "user" ? "你" : agentName}</div>
              <div className="message-content"><ReactMarkdown remarkPlugins={[remarkGfm]}>{message.content || "…"}</ReactMarkdown></div>
            </article>
          ))}
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
          <div className="chat-composer">
            <textarea value={input} onChange={event => setInput(event.target.value)} placeholder="发送到云端 Agent" disabled={!active || sending || waitingForResponse} onKeyDown={event => { if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); sendMessage(); } }} />
            <div className="chat-composer-footer"><span>AK/SK 仅保留在本地 Studio 进程</span><button className="icon-button primary" type="button" disabled={!input.trim() || sending || waitingForResponse || !active} onClick={sendMessage} aria-label="发送"><Send size={17} /></button></div>
          </div>
        </div>
      </div>
    </section>
  );
}

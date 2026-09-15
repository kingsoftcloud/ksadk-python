import { createPortal } from "react-dom";
import { useCallback, useEffect, useImperativeHandle, useMemo, useRef, useState, type Ref } from "react";
import { Bot, MessageSquarePlus, PanelLeftOpen, Trash2, X } from "lucide-react";
import { AgentConversationTimeline } from "@kingsoftcloud/ksadk-web/chat/timeline";
import { AgentConversationComposer } from "@kingsoftcloud/ksadk-web/chat/composer";
import { useAgentChat } from "@kingsoftcloud/ksadk-web/hooks";
import { ApiFacadeImpl } from "@kingsoftcloud/ksadk-web/runtime";
import { apiFetch } from "../api";
import { AgentAvatar, type AgentAppearance } from "./AgentAvatar";
import { ConfirmDialog } from "./ConfirmDialog";
import { CompactHarnessTimeline } from "./CompactHarnessTimeline";
import { useRunDocumentActions } from "./RunDocumentActions";
import { ConversationController, type ConversationId } from "@kingsoftcloud/ksadk-web/conversation";

export interface ChatWorkspaceHandle { startNewChat: () => void; }

interface ChatWorkspaceProps {
  ref?: Ref<ChatWorkspaceHandle>;
  newChatRequest?: number;
  onNewChatStarted?: () => void;
  integratedHistory?: boolean;
  onStreamingChange?: (streaming: boolean) => void;
  historyHost?: HTMLElement | null;
  headerHost?: HTMLElement | null;
  onSelectConversation?: () => void;
  agentId: string;
  agentName: string;
  agentAppearance?: AgentAppearance;
  active?: boolean;
  refreshTick?: number;
  requestedSessionId?: string;
  onSessionChanged?: (sessionId: string) => void;
  onRunChanged?: () => void;
  onConfigureAgent?: () => void;
  onOpenSettings?: () => void;
}

function formatSessionTime(value: string): string {
  const parsed = Date.parse(value);
  if (!Number.isFinite(parsed)) return "";
  const diff = Date.now() - parsed;
  if (diff < 60_000) return "刚刚";
  if (diff < 3_600_000) return `${Math.floor(diff / 60_000)} 分钟前`;
  if (diff < 86_400_000) return `${Math.floor(diff / 3_600_000)} 小时前`;
  return new Date(parsed).toLocaleDateString();
}

function shortText(value: string, limit = 34): string {
  return value.length > limit ? `${value.slice(0, limit)}…` : value;
}

function sessionIsRunning(status?: string): boolean {
  return ["RUNNING", "WAITING_INPUT", "PAUSED"].includes(String(status || "").toUpperCase());
}

function sessionDisplayTitle(session: { SessionId: string; Title?: string }): string {
  const title = String(session.Title || "").trim();
  return !title || title === session.SessionId ? "新会话" : title;
}

/**
 * Studio keeps only its product shell here. Conversation state, replay,
 * streaming, thinking/tool rendering, approvals, HITL, attachments, and the
 * composer all come from ksadk-web.
 */
export function ChatWorkspace({
  agentId,
  agentName,
  agentAppearance,
  active = true,
  refreshTick = 0,
  requestedSessionId = "",
  onSessionChanged,
  newChatRequest = 0, onNewChatStarted,
  ref, integratedHistory = false, onStreamingChange, historyHost, headerHost, onSelectConversation,
}: ChatWorkspaceProps) {
  const api = useMemo(() => new ApiFacadeImpl({ fetch: apiFetch, agentId }), [agentId]);
  const conversationController = useRef(new ConversationController());
  const chat = useAgentChat({ api, agentId, conversationClient: null, conversationController: conversationController.current });
  const documents = useRunDocumentActions();
  const compactTimeline = chat.uiCapabilities.ConversationPresentation?.Timeline === "compact";
  const Timeline = compactTimeline ? CompactHarnessTimeline : AgentConversationTimeline;
  const startedNewChatRequest = useRef(0);
  // facade 的 createNewSession 没有在途去重：连点"新对话"会连发
  // CreateSession 产生多条空会话。这里统一加互斥，创建完成后才允许下一次。
  const creatingSession = useRef(false);
  // 连点"新对话"复用刚自动创建且仍为空的会话（而不是再建一条）；用户
  // 一旦在其中发了消息或切到其他会话，即恢复正常新建。
  const autoCreatedEmptySessionId = useRef<string | null>(null);
  const currentSessionIdRef = useRef<string | null>(null);
  const openedRequest = useRef("");
  const currentRequest = useRef("");
  const conversationIdFor = useCallback((sessionId: string | null) => {
    return chat.conversationId || conversationController.current.getOrCreate(agentId, sessionId);
  }, [agentId, chat.conversationId]);
  useEffect(() => { currentSessionIdRef.current = chat.currentSessionId; }, [chat.currentSessionId]);
  useEffect(() => {
    // A target change invalidates pending reads, while the runtime task keeps
    // running in the broker. This is intentionally separate from aborting the
    // execution subscription.
    conversationController.current.navigate();
    openedRequest.current = "";
  }, [agentId]);
  useEffect(() => {
    if (chat.messages?.length) autoCreatedEmptySessionId.current = null;
  }, [chat.messages]);
  const guardedCreateNewSession = useCallback(async () => {
    if (creatingSession.current || chat.isStreaming) return;
    if (currentSessionIdRef.current
      && currentSessionIdRef.current === autoCreatedEmptySessionId.current) return;
    creatingSession.current = true;
    try {
      if (chat.startNewConversation) chat.startNewConversation();
      else await chat.createNewSession();
      autoCreatedEmptySessionId.current = currentSessionIdRef.current;
    } finally {
      creatingSession.current = false;
    }
  }, [chat.isStreaming, chat.startNewConversation, chat.createNewSession]);
  useEffect(() => {
    if (!newChatRequest) { startedNewChatRequest.current = 0; return; }
    if (!active || chat.bootstrapStatus !== "ready" || chat.agentId !== agentId
      || chat.isLoadingSessions || startedNewChatRequest.current === newChatRequest) return;
    startedNewChatRequest.current = newChatRequest;
    void guardedCreateNewSession().finally(() => onNewChatStarted?.());
  }, [active, agentId, newChatRequest, onNewChatStarted, chat.bootstrapStatus, chat.agentId, chat.isLoadingSessions, guardedCreateNewSession]);
  currentRequest.current = active && requestedSessionId ? `${agentId}:${requestedSessionId}` : "";
  useEffect(() => {
    if (!requestedSessionId) { openedRequest.current = ""; return; }
    const request = `${agentId}:${requestedSessionId}`;
    if (!active || chat.bootstrapStatus !== "ready" || chat.agentId !== agentId || chat.isLoadingSessions || openedRequest.current === request) return;
    openedRequest.current = request;
    const requestEpoch = conversationController.current.navigate();
    void (async () => {
      await chat.refresh();
      if (currentRequest.current === request
        && conversationController.current.navigationEpoch === requestEpoch) chat.selectSession(requestedSessionId);
    })();
  }, [active, agentId, requestedSessionId, chat.bootstrapStatus, chat.agentId, chat.isLoadingSessions, chat.selectSession, chat.refresh]);
  const [query, setQuery] = useState("");
  const [sessionPanelOpen, setSessionPanelOpen] = useState(false);
  const sessionTriggerRef = useRef<HTMLButtonElement>(null);
  const sessionSearchRef = useRef<HTMLInputElement>(null);
  const [deleteSessionId, setDeleteSessionId] = useState("");
  const [deleting, setDeleting] = useState(false);
  const isStreamingRef = useRef(false);
  useEffect(() => { isStreamingRef.current = chat.isStreaming; }, [chat.isStreaming]);
  const previousRefreshTick = useRef(refreshTick);
  const refreshChat = chat.refresh;
  const previousTransport = useRef({ agentId, sessionId: chat.currentSessionId, streaming: false });
  // 提交→SSE 首帧之间存在会话创建/运行时预热窗口，此时 facade 的
  // isStreaming 仍为 false；用本地 pending 让"正在思考"流光即时出现。
  const [submitPending, setSubmitPending] = useState(false);
  useEffect(() => {
    if (chat.isStreaming) setSubmitPending(false);
  }, [chat.isStreaming]);
  useEffect(() => {
    if (!submitPending) return;
    const timer = window.setTimeout(() => setSubmitPending(false), 60000);
    return () => window.clearTimeout(timer);
  }, [submitPending]);

  useEffect(() => {
    onSessionChanged?.(chat.currentSessionId || "");
    return () => onSessionChanged?.("");
  }, [chat.currentSessionId, onSessionChanged]);

  useEffect(() => {
    onStreamingChange?.(chat.isStreaming);
    return () => onStreamingChange?.(false);
  }, [chat.isStreaming, onStreamingChange]);

  // 进入/切换会话时后台预热 harness Provider 激活（MCP spawn/health/list
  // ~12s），把这段开销移到用户输入之前；失败静默，首轮照旧现场预热。
  useEffect(() => {
    if (!chat.uiCapabilities.RuntimePrewarm || chat.bootstrapStatus !== "ready"
      || !chat.currentSessionId) return;
    const controller = new AbortController();
    void apiFetch(
      `/api/v1/agents/${encodeURIComponent(agentId)}/runtime:prewarm`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ sessionId: chat.currentSessionId }),
        signal: controller.signal,
      },
    ).catch(() => {});
    return () => controller.abort();
  }, [agentId, chat.uiCapabilities.RuntimePrewarm, chat.bootstrapStatus, chat.currentSessionId]);

  // facade bootstrap 会自动 adopt 最近会话；切 Agent 应回到初始对话框，
  // 仅当路由显式要求打开某会话时才保留。只在 bootstrap 完成时执行一次。
  const initialSelectionHandled = useRef(false);
  useEffect(() => {
    if (chat.bootstrapStatus !== "ready" || initialSelectionHandled.current) return;
    initialSelectionHandled.current = true;
    if (requestedSessionId) return;
    // facade bootstrap 会自动 adopt 最近会话（React state 此刻可能尚未更新），
    // 因此无条件回到初始对话框；路由显式指定会话时除外。
    chat.selectSession(null);
  }, [chat.bootstrapStatus, requestedSessionId, chat.selectSession]);

  useImperativeHandle(ref, () => ({ startNewChat() {
    void guardedCreateNewSession();
  } }), [guardedCreateNewSession]);

  function closeSessionPanel() {
    setSessionPanelOpen(false);
    sessionTriggerRef.current?.focus();
  }

  useEffect(() => {
    if (sessionPanelOpen) sessionSearchRef.current?.focus();
  }, [sessionPanelOpen]);

  useEffect(() => {
    const previous = previousTransport.current;
    const settled = previous.agentId === agentId
      && previous.sessionId === chat.currentSessionId
      && previous.streaming && !chat.isStreaming;
    previousTransport.current = { agentId, sessionId: chat.currentSessionId, streaming: chat.isStreaming };
    // Reconcile the durable transcript after every transport settles, including
    // a clean EOF without response.completed. If the run remains active, the
    // shared session loader resumes its event subscription without re-running it.
    if (settled) void refreshChat();
  }, [agentId, chat.currentSessionId, chat.isStreaming, refreshChat]);

  const currentSession = chat.sessions.find(session => session.SessionId === chat.currentSessionId);
  const conversationTitle = currentSession && currentSession.Title !== currentSession.SessionId
    ? currentSession.Title || "新对话" : "新对话";

  // 流式运行期间冻结会话列表顺序：isStreaming 翻转会触发 refresh 重排序，
  // 正在阅读/点击列表时行位置乱跳；运行结束后才允许按最新 UpdatedAt 排序。
  const frozenOrder = useRef<string[] | null>(null);
  const orderedSessions = useMemo(() => {
    if (!chat.isStreaming) {
      frozenOrder.current = null;
      return chat.sessions;
    }
    if (!frozenOrder.current) frozenOrder.current = chat.sessions.map(s => s.SessionId);
    const frozenIds = frozenOrder.current;
    const byId = new Map(chat.sessions.map(session => [session.SessionId, session]));
    const frozen = frozenIds.map(id => byId.get(id)).filter(Boolean) as typeof chat.sessions;
    const extra = chat.sessions.filter(session => !frozenIds.includes(session.SessionId));
    return [...frozen, ...extra];
  }, [chat.sessions, chat.isStreaming]);
  const filteredSessions = useMemo(() => {
    const keyword = query.trim().toLowerCase();
    if (!keyword) return orderedSessions;
    return orderedSessions.filter(session => (
      sessionDisplayTitle(session)
    ).toLowerCase().includes(keyword));
  }, [orderedSessions, query]);

  useEffect(() => {
    if (previousRefreshTick.current === refreshTick) return;
    previousRefreshTick.current = refreshTick;
    void refreshChat();
  }, [refreshChat, refreshTick]);

  const conversationHeader = (
        <div className="chat-conversation-header">
            {!integratedHistory && <button
              ref={sessionTriggerRef}
            className="icon-button tertiary chat-session-mobile-trigger"
            type="button"
            aria-label="打开会话历史"
            title="会话历史"
            aria-expanded={sessionPanelOpen}
            onClick={() => setSessionPanelOpen(true)}
          >
            <PanelLeftOpen size={17} />
          </button>}
          {!integratedHistory && <AgentAvatar name={agentName} appearance={agentAppearance} size="sm" />}
          <h1>{conversationTitle}</h1>
        </div>
  );

  const history = (
      <aside className={`chat-session-sidebar${integratedHistory ? " integrated-history" : ""}`} aria-label="会话历史" onKeyDown={event => {
        if (sessionPanelOpen && event.key === "Escape") {
          event.preventDefault();
          closeSessionPanel();
        }
      }}>
        <header className="chat-session-header">
          <h2>{integratedHistory ? "最近对话" : "会话"}</h2>
          {!integratedHistory && <div className="chat-session-header-actions">
            <button
              className="icon-button tertiary"
              type="button"
              aria-label="新对话"
              title="新对话"
              onClick={() => { void guardedCreateNewSession(); if (sessionPanelOpen) closeSessionPanel(); }}
              disabled={chat.isStreaming}
            >
              <MessageSquarePlus size={16} />
            </button>
            <button
              className="icon-button tertiary chat-session-mobile-close"
              type="button"
              aria-label="关闭会话历史"
              title="关闭会话历史"
              onClick={closeSessionPanel}
            >
              <X size={17} />
            </button>
          </div>}
        </header>
        <label className="chat-session-search">
          <span className="sr-only">搜索会话</span>
          <input
            ref={sessionSearchRef}
            type="search"
            value={query}
            onChange={event => setQuery(event.target.value)}
            placeholder="搜索会话"
          />
        </label>
        <div
          className="chat-session-list"
          onScroll={event => {
            const element = event.currentTarget;
            if (element.scrollHeight - element.scrollTop - element.clientHeight < 200) {
              void chat.loadMoreSessions();
            }
          }}
        >
          {chat.isLoadingSessions && chat.sessions.length === 0 ? (
            <div className="chat-session-skeleton" aria-label="正在加载会话" role="status">
              <i /><i /><i />
            </div>
          ) : filteredSessions.length === 0 ? (
            <div className="session-empty">{query ? "没有匹配的会话" : "还没有会话"}</div>
          ) : filteredSessions.map(session => {
            const running = sessionIsRunning(session.ActiveRunStatus);
            const displayTitle = sessionDisplayTitle(session);
            return (
              <div
                key={session.SessionId}
                className={`chat-session-item${chat.currentSessionId === session.SessionId ? " active" : ""}${running ? " running" : ""}`}
              >
                <button
                  className="chat-session-main"
                  type="button"
                  aria-current={chat.currentSessionId === session.SessionId ? "true" : undefined}
                  onClick={() => { chat.selectSession(session.SessionId); onSelectConversation?.(); if (sessionPanelOpen) closeSessionPanel(); }}
                  title={`${displayTitle} · ${formatSessionTime(String(session.UpdatedAt || ""))}`}
                >
                  <strong>{shortText(displayTitle)}</strong>
                  {running ? <span className="session-status running" aria-label="运行中" /> : null}
                </button>
                <button
                  className="chat-session-delete"
                  type="button"
                  aria-label={`删除会话：${shortText(displayTitle)}`}
                  title={running ? "运行中不可删除" : "删除会话"}
                  disabled={running}
                  onClick={() => setDeleteSessionId(session.SessionId)}
                >
                  <Trash2 size={14} />
                </button>
              </div>
            );
          })}
        </div>
      </aside>
  );

  return (
    <div
      className={`ksadk-web studio-chat-shell${sessionPanelOpen ? " sessions-open" : ""}`}
      data-testid="studio-chat-workbench"
      data-agent-id={agentId}
      data-integrated-history={integratedHistory}
      data-integrated-header={Boolean(headerHost)}
      onClickCapture={documents.onClickCapture}
      onContextMenuCapture={documents.onContextMenuCapture}
    >
      {integratedHistory ? (historyHost ? createPortal(history, historyHost) : null) : history}


      <button
        className="chat-session-backdrop"
        type="button"
        aria-label="关闭会话历史"
        onClick={closeSessionPanel}
      />

      <section className="chat-conversation" aria-label={`与 ${agentName} 对话`}>
        {headerHost ? (active ? createPortal(conversationHeader, headerHost) : null) : conversationHeader}

        {chat.bootstrapStatus === "loading" || newChatRequest !== 0 ? (
          <div className="chat-bootstrap-loading" role="status" aria-label="正在连接 Agent">
            <i />
          </div>
        ) : chat.bootstrapStatus !== "ready" ? (
          <div className="chat-empty" role="alert">
            <span className="chat-empty-icon"><Bot size={22} /></span>
            <h2>会话暂不可用</h2>
            <p>{chat.bootstrapErrorMessage || "Agent 会话能力未开启。"}</p>
          </div>
        ) : (
          <>
            <Timeline
              className="studio-chat-timeline"
              agentName={agentName}
              messages={chat.messages}
              isStreaming={chat.isStreaming || submitPending}
              activity={chat.activity}
              sessionId={chat.currentSessionId}
              hasMoreMessages={(chat.messages?.length ?? 0) >= 50}
              emptyState={(
                <div className="studio-conversation-welcome">
                  <p>{agentName}</p>
                  <h2>有什么可以帮你？</h2>
                </div>
              )}
              isMobile={chat.isMobile}
              onDeleteFeedback={chat.deleteResponseFeedback}
              onSubmitFeedback={chat.submitResponseFeedback}
              onRespondToApproval={chat.respondToApproval}
              onSubmitAguiAction={chat.submitAguiAction}
              onStopGeneration={chat.stop}
              onCancelRemote={chat.uiCapabilities.StopRun ? chat.cancelRemote : undefined}
              onLoadOlderSessionMessages={chat.loadOlderMessages}
              interactionRecords={chat.interactionRecords}
            />
            {!compactTimeline
              && (chat.isStreaming || submitPending)
              && !(chat.messages?.length
                && chat.messages[chat.messages.length - 1].role === "model") ? (
              <div className="harness-thinking" role="status">
                <span className="text-shimmer">正在思考…</span>
              </div>
            ) : null}
            <div className="studio-composer-area">
            <AgentConversationComposer
              draftKey={conversationIdFor(chat.currentSessionId)}
              draftStore={chat.conversationDrafts}
              onCompactContext={chat.uiCapabilities.ContextCompaction ? chat.compactContext : undefined}
              composerMaxHeight={176}
              submitDraft={async (text, attachments, _responsesInput, _previousResponseId, executionMode) => {
                setSubmitPending(true);
                chat.send(text, { attachments, executionMode });
              }}
              stopGeneration={chat.stop}
              cancelRemote={chat.uiCapabilities.StopRun ? chat.cancelRemote : undefined}
              isMobile={chat.isMobile}
              attachmentsEnabled={chat.uiCapabilities.Attachments !== false}
              approvalEnabled={Boolean(chat.uiCapabilities.Approval)}
              approvalPolicy={chat.uiCapabilities.ApprovalPolicy}
              thinkingEnabled={Boolean(chat.uiCapabilities.Thinking)}
              runtimeCapabilityMatrix={chat.uiCapabilities.RuntimeCapabilityMatrix}
              pendingInteractions={chat.pendingInteractions}
              onRespondInteraction={input => { void chat.respondInteraction(input); }}
              localCatalog={chat.localCatalog}
            />
            </div>
          </>
        )}
      </section>

      {deleteSessionId ? (
        <ConfirmDialog
          title="删除这个会话？"
          description="删除后该会话的历史消息将无法恢复。"
          confirmText={deleting ? "处理中…" : "删除"}
          busy={deleting}
          onConfirm={() => {
            const id = deleteSessionId;
            setDeleteSessionId("");
            setDeleting(true);
            void (async () => {
              // 运行中的会话服务端拒绝删除（409）；先停止当前运行，
              // 等它退出（最多 ~10s）再删，避免"处理中"卡到流式结束。
              if (isStreamingRef.current && currentSessionIdRef.current === id) {
                // 仅当删除的就是当前流式会话：stop 只断开前端订阅，服务端
                // run 仍 RUNNING；cancelRemote 才会把 run 落为 canceled。
                void chat.cancelRemote().catch(() => {});
                chat.stop();
                for (let i = 0; i < 40 && isStreamingRef.current; i += 1) {
                  await new Promise(resolve => setTimeout(resolve, 250));
                }
              }
              // 执行 host 对刚结束运行的会话有占用锁，run 终态落盘与锁释放
              // 有延迟；给足重试窗口，避免"点了没反应"。
              for (let attempt = 0; attempt < 10; attempt += 1) {
                try {
                  await chat.deleteSession(id);
                  break;
                } catch {
                  if (attempt === 9) break;
                  await new Promise(resolve => setTimeout(resolve, 800));
                }
              }
              setDeleting(false);
            })();
          }}
          onCancel={() => setDeleteSessionId("")}
        />
      ) : null}

      {!active ? <span hidden data-testid="studio-chat-inactive" /> : null}
      {documents.ui}
    </div>
  );
}

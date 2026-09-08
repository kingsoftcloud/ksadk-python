import { useEffect, useMemo, useRef, useState } from "react";
import { Bot, MessageSquarePlus, PanelLeftOpen, Trash2, X } from "lucide-react";
import { AgentConversationTimeline } from "@kingsoftcloud/ksadk-web/chat/timeline";
import { AgentConversationComposer } from "@kingsoftcloud/ksadk-web/chat/composer";
import { useAgentChat } from "@kingsoftcloud/ksadk-web/hooks";
import { ApiFacadeImpl } from "@kingsoftcloud/ksadk-web/runtime";
import { apiFetch } from "../api";
import { AgentAvatar, type AgentAppearance } from "./AgentAvatar";
import { ConfirmDialog } from "./ConfirmDialog";

interface ChatWorkspaceProps {
  agentId: string;
  agentName: string;
  agentAppearance?: AgentAppearance;
  active?: boolean;
  refreshTick?: number;
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
}: ChatWorkspaceProps) {
  const api = useMemo(() => new ApiFacadeImpl({ fetch: apiFetch, agentId }), [agentId]);
  const chat = useAgentChat({ api, agentId, conversationClient: null });
  const [query, setQuery] = useState("");
  const [sessionPanelOpen, setSessionPanelOpen] = useState(false);
  const [deleteSessionId, setDeleteSessionId] = useState("");
  const previousRefreshTick = useRef(refreshTick);
  const refreshChat = chat.refresh;
  const previousTransport = useRef({ agentId, sessionId: chat.currentSessionId, streaming: false });

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

  const filteredSessions = useMemo(() => {
    const keyword = query.trim().toLowerCase();
    if (!keyword) return chat.sessions;
    return chat.sessions.filter(session => (
      session.Title || session.SessionId
    ).toLowerCase().includes(keyword));
  }, [chat.sessions, query]);

  useEffect(() => {
    if (previousRefreshTick.current === refreshTick) return;
    previousRefreshTick.current = refreshTick;
    void refreshChat();
  }, [refreshChat, refreshTick]);

  return (
    <div
      className={`ksadk-web studio-chat-shell${sessionPanelOpen ? " sessions-open" : ""}`}
      data-testid="studio-chat-workbench"
      data-agent-id={agentId}
    >
      <aside className="chat-session-sidebar" aria-label="会话历史">
        <header className="chat-session-header">
          <h2>会话</h2>
          <div className="chat-session-header-actions">
            <button
              className="icon-button tertiary"
              type="button"
              aria-label="新对话"
              title="新对话"
              onClick={() => { void chat.createNewSession(); }}
              disabled={chat.isStreaming}
            >
              <MessageSquarePlus size={16} />
            </button>
            <button
              className="icon-button tertiary chat-session-mobile-close"
              type="button"
              aria-label="关闭会话历史"
              title="关闭会话历史"
              onClick={() => setSessionPanelOpen(false)}
            >
              <X size={17} />
            </button>
          </div>
        </header>
        <label className="chat-session-search">
          <span className="sr-only">搜索会话</span>
          <input
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
            return (
              <div
                key={session.SessionId}
                className={`chat-session-item${chat.currentSessionId === session.SessionId ? " active" : ""}${running ? " running" : ""}`}
              >
                <button
                  className="chat-session-main"
                  type="button"
                  aria-current={chat.currentSessionId === session.SessionId ? "true" : undefined}
                  onClick={() => chat.selectSession(session.SessionId)}
                  title={`${session.Title || session.SessionId} · ${formatSessionTime(String(session.UpdatedAt || ""))}`}
                >
                  <strong>{shortText(session.Title || session.SessionId)}</strong>
                  {running ? <span className="session-status running" aria-label="运行中" /> : null}
                </button>
                <button
                  className="chat-session-delete"
                  type="button"
                  aria-label={`删除会话：${shortText(session.Title || session.SessionId)}`}
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

      <button
        className="chat-session-backdrop"
        type="button"
        aria-label="关闭会话历史"
        onClick={() => setSessionPanelOpen(false)}
      />

      <section className="chat-conversation" aria-label={`与 ${agentName} 对话`}>
        <header className="chat-conversation-header">
          <button
            className="icon-button tertiary chat-session-mobile-trigger"
            type="button"
            aria-label="打开会话历史"
            title="会话历史"
            aria-expanded={sessionPanelOpen}
            onClick={() => setSessionPanelOpen(true)}
          >
            <PanelLeftOpen size={17} />
          </button>
          <AgentAvatar name={agentName} appearance={agentAppearance} size="sm" />
          <h1>{agentName}</h1>
        </header>

        {chat.bootstrapStatus === "loading" ? (
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
            <AgentConversationTimeline
              agentName={agentName}
              emptyState={(
                <div className="studio-conversation-welcome">
                  <AgentAvatar name={agentName} appearance={agentAppearance} size="lg" />
                  <h2>{agentName}</h2>
                  <p>有什么可以帮你？</p>
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
            <AgentConversationComposer
              onCompactContext={chat.uiCapabilities.ContextCompaction ? chat.compactContext : undefined}
              composerMaxHeight={176}
              submitDraft={async (text, attachments, _responsesInput, _previousResponseId, executionMode) => {
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
          </>
        )}
      </section>

      {deleteSessionId ? (
        <ConfirmDialog
          title="删除这个会话？"
          description="删除后该会话的历史消息将无法恢复。"
          confirmText="删除"
          busy={chat.isStreaming}
          onConfirm={() => {
            const id = deleteSessionId;
            setDeleteSessionId("");
            void chat.deleteSession(id);
          }}
          onCancel={() => setDeleteSessionId("")}
        />
      ) : null}

      {!active ? <span hidden data-testid="studio-chat-inactive" /> : null}
    </div>
  );
}

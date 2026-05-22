import { useCallback, useEffect, useRef } from 'react';
import { useSessionStore } from '../stores/session.js';
import { useMessageStore } from '../stores/message.js';
import { useStreamingStore } from '../stores/streaming.js';
import { useUIStore } from '../stores/ui.js';
import { CancelledError } from '../api/client.js';
import { findActiveRunIds } from '../utils/run-state.js';
import { buildMessagesFromSessionEvents, eventHasTerminalRunStatus, maxSeqIdFromEvents } from '../utils/session-events.js';
import { shouldRenderFeedbackControls, normalizeFeedback } from '../utils/feedback.js';
import { readPersistedSessionId, resolveSessionToRestore } from '../utils/session.js';
import { upsertSessions } from '../utils/session-helpers.js';
import type { Message, Session } from '../components/chat/types.js';
import type { SessionEventRecord } from '../types/session-events.js';
import type { UiCapabilities } from '../types/capabilities.js';
import type { ApiFacade } from '../core/api/types.js';

const RESTORE_IDLE_NOTICE_MS = 12_000;
const RESTORE_SUBSCRIPTION_TIMEOUT_MS = 90_000;

type SessionLifecycleContext = {
  agentId: string;
  currentSessionId: string | null;
  isStreaming: boolean;
  isMobile: boolean;
  uiCapabilities: UiCapabilities;
  api: ApiFacade;
  resetCompaction: () => void;
};

export function useSessionLifecycle(ctx: SessionLifecycleContext) {
  const {
    agentId,
    api,
    isMobile,
    isStreaming,
    resetCompaction,
    uiCapabilities,
  } = ctx;
  const currentSessionIdRef = useRef<string | null>(ctx.currentSessionId);
  const agentIdRef = useRef(ctx.agentId);
  const runSubscriptionAbortRef = useRef<AbortController | null>(null);
  const loadSessionRef = useRef<((sessionId: string) => Promise<void>) | null>(null);
  const fetchSessionsRef = useRef<
    ((
      targetAgentId?: string,
      preferredSessionId?: string | null,
    ) => Promise<void>) | null
  >(null);

  const loadFeedbackForMessages = useCallback(
    async (targetAgentId: string, sessionId: string, history: Message[]) => {
      const targets = history.filter((message) =>
        shouldRenderFeedbackControls(message, false, false),
      );
      if (!targets.length) {
        return;
      }

      const entries = await Promise.all(
        targets.map(async (message) => {
          try {
            const data = await api.getResponseFeedback({
              AgentId: targetAgentId,
              SessionId: sessionId,
              ResponseId: message.responseId,
              EventId: message.eventId,
            });
            const rawData = data as Record<string, unknown> | null;
            const feedbackData = rawData?.Feedback
              ? normalizeFeedback(rawData.Feedback)
              : null;
            return feedbackData ? { messageId: message.id, feedback: feedbackData } : null;
          } catch (error) {
            console.error('Failed to load response feedback:', error);
            return null;
          }
        }),
      );

      if (currentSessionIdRef.current !== sessionId) {
        return;
      }
      const feedbackByMessageId = new Map(
        entries
          .filter(
            (entry): entry is { messageId: string; feedback: NonNullable<Message['feedback']> } =>
              Boolean(entry),
          )
          .map((entry) => [entry.messageId, entry.feedback]),
      );
      if (!feedbackByMessageId.size) {
        return;
      }
      useMessageStore.getState().patchMessages((prev) =>
        prev.map((message) =>
          feedbackByMessageId.has(message.id)
            ? { ...message, feedback: feedbackByMessageId.get(message.id) }
            : message,
        ),
      );
    },
    [api],
  );

  const subscribeRunEvents = useCallback(
    async (options: {
      sessionId: string;
      invocationId: string;
      afterSeqId: number;
    }) => {
      runSubscriptionAbortRef.current?.abort();
      const controller = new AbortController();
      runSubscriptionAbortRef.current = controller;
      useStreamingStore.getState().setStreaming(true);
      useStreamingStore.getState().beginActivity({
        runId: options.invocationId,
        source: 'restore',
        status: 'connecting',
        phase: '恢复运行事件订阅',
        detail: '页面刷新后正在连接未完成的运行。',
      });
      let shouldReloadSession = false;
      let eventCount = 0;
      let terminalStatusSeen = false;
      let idleNoticeTimer: ReturnType<typeof globalThis.setTimeout> | null = null;
      const timeoutTimer = globalThis.setTimeout(() => {
        if (runSubscriptionAbortRef.current !== controller || controller.signal.aborted) {
          return;
        }
        useStreamingStore.getState().updateActivity({
          status: 'stopped',
          phase: '恢复订阅超时',
          detail: '没有收到新的运行事件，已解除前端运行锁定；后台状态可刷新会话后再确认。',
          countEvent: false,
        });
        controller.abort();
      }, RESTORE_SUBSCRIPTION_TIMEOUT_MS);
      const armIdleNotice = () => {
        if (idleNoticeTimer) {
          globalThis.clearTimeout(idleNoticeTimer);
        }
        idleNoticeTimer = globalThis.setTimeout(() => {
          if (runSubscriptionAbortRef.current !== controller || controller.signal.aborted) {
            return;
          }
          useStreamingStore.getState().updateActivity({
            status: 'waiting',
            phase: '仍在等待运行时输出',
            detail: '运行尚未返回新的事件。Hermes 长任务建议打开 TUI 查看实时终端。',
            countEvent: false,
          });
        }, RESTORE_IDLE_NOTICE_MS);
      };

      try {
        const stream = await api.subscribeRunEvents(
          {
            sessionId: options.sessionId,
            invocationId: options.invocationId,
            afterSeqId: options.afterSeqId,
          },
          { signal: controller.signal },
        );
        const reader = stream.getReader();
        const decoder = new TextDecoder();
        let buffer = '';
        let replayedEvents: SessionEventRecord[] = [];
        useStreamingStore.getState().updateActivity({
          status: 'waiting',
          phase: '等待恢复事件',
          countEvent: false,
        });
        armIdleNotice();

        while (!terminalStatusSeen) {
          const { value, done } = await reader.read();
          if (done) break;
          armIdleNotice();
          buffer += decoder.decode(value, { stream: true });
          const chunks = buffer.split('\n\n');
          buffer = chunks.pop() || '';

          for (const chunk of chunks) {
            if (!chunk.trim()) continue;
            const dataLines: string[] = [];
            for (const line of chunk.split('\n')) {
              if (line.startsWith('data:')) {
                dataLines.push(line.substring(5).trim());
              }
            }
            const dataString = dataLines.join('\n').trim();
            if (!dataString || dataString === '[DONE]') {
              terminalStatusSeen = dataString === '[DONE]';
              shouldReloadSession = shouldReloadSession || terminalStatusSeen;
              continue;
            }
            try {
              const event = JSON.parse(dataString) as SessionEventRecord;
              eventCount += 1;
              useStreamingStore.getState().updateActivity({
                status: eventHasTerminalRunStatus(event) ? 'completed' : 'running',
                phase: event.EventType === 'run_status'
                  ? `运行状态：${String(event.Content?.status || '更新')}`
                  : `收到事件：${event.EventType || 'conversation.event'}`,
              });
              replayedEvents = [...replayedEvents, event];
              terminalStatusSeen = terminalStatusSeen || eventHasTerminalRunStatus(event);
              shouldReloadSession = shouldReloadSession || terminalStatusSeen;
              useMessageStore.getState().patchMessages((prev) => {
                const current = buildMessagesFromSessionEvents([
                  ...replayedEvents.filter(
                    (item) => item.EventId && prev.every((message) => message.id !== item.EventId),
                  ),
                ]);
                if (!current.length) {
                  return prev;
                }
                return [...prev, ...current];
              });
            } catch (error) {
              console.warn('Failed to parse run event data', dataString, error);
            }
          }
        }
      } catch (error) {
        const isAbortError = error instanceof DOMException && error.name === 'AbortError';
        if (!isAbortError) {
          console.error('Failed to subscribe run events:', error);
          useStreamingStore.getState().updateActivity({
            status: 'failed',
            phase: '恢复订阅失败',
            detail: error instanceof Error ? error.message : String(error),
            countEvent: false,
          });
        } else if (useStreamingStore.getState().activity?.status !== 'stopped') {
          useStreamingStore.getState().stopActivity('已停止接收恢复事件。后台运行可能仍在继续。');
        }
      } finally {
        globalThis.clearTimeout(timeoutTimer);
        if (idleNoticeTimer) {
          globalThis.clearTimeout(idleNoticeTimer);
        }
        if (runSubscriptionAbortRef.current === controller) {
          runSubscriptionAbortRef.current = null;
        }
        useStreamingStore.getState().setStreaming(false);
        if (terminalStatusSeen) {
          useStreamingStore.getState().updateActivity({
            status: 'completed',
            phase: '恢复订阅结束',
            countEvent: false,
          });
        } else if (useStreamingStore.getState().activity?.status !== 'stopped') {
          useStreamingStore.getState().updateActivity({
            status: 'stopped',
            phase: eventCount === 0 ? '没有收到新的运行事件' : '恢复订阅提前结束',
            detail: '已解除前端运行锁定；如果后台仍在执行，可以刷新会话或打开 TUI 确认。',
            countEvent: false,
          });
        }
        if (shouldReloadSession && currentSessionIdRef.current === options.sessionId) {
          void loadSessionRef.current?.(options.sessionId);
        }
        void fetchSessionsRef.current?.(agentIdRef.current, options.sessionId);
      }
    },
    [api],
  );

  const loadSession = useCallback(
    async (sessionId: string) => {
      currentSessionIdRef.current = sessionId;
      useSessionStore.getState().setCurrentSessionId(sessionId);
      resetCompaction();
      runSubscriptionAbortRef.current?.abort();
      useStreamingStore.getState().clearActivity();
      if (isMobile) {
        useUIStore.getState().setMobileSidebarOpen(false);
      }

      try {
        const data = await api.listSessionEvents(sessionId);
        const eventsData = data as { Events?: SessionEventRecord[] };
        if (eventsData?.Events) {
          const events = eventsData.Events;
          const history = buildMessagesFromSessionEvents(events);
          useMessageStore.getState().setMessages(history);
          void loadFeedbackForMessages(agentIdRef.current, sessionId, history);
          const activeRuns = findActiveRunIds(events, {
            now: Date.now(),
            staleAfterMs: 30 * 60 * 1000,
          });
          const lastSeqId = maxSeqIdFromEvents(events);
          if (
            uiCapabilities.RunLifecycle.Enabled &&
            uiCapabilities.RunLifecycle.Resume &&
            activeRuns[0]
          ) {
            void subscribeRunEvents({
              sessionId,
              invocationId: activeRuns[0],
              afterSeqId: lastSeqId,
            });
          }
        } else {
          useMessageStore.getState().setMessages([]);
        }
      } catch (error) {
        console.error('Failed to load session events:', error);
      }
    },
    [
      api,
      isMobile,
      loadFeedbackForMessages,
      resetCompaction,
      subscribeRunEvents,
      uiCapabilities.RunLifecycle.Enabled,
      uiCapabilities.RunLifecycle.Resume,
    ],
  );

  const fetchSessions = useCallback(
    async (
      targetAgentId = 'default-agent',
      preferredSessionId: string | null = null,
    ) => {
      try {
        const sessions = await api.listSessions(targetAgentId);
        const sorted = upsertSessions(useSessionStore.getState().sessions, sessions as Session[]);
        useSessionStore.getState().setSessions(sorted);
        const activeSessionId = currentSessionIdRef.current;
        const restoredSessionId = resolveSessionToRestore(
          sorted,
          activeSessionId || preferredSessionId || readPersistedSessionId(targetAgentId),
        );
        if (restoredSessionId && restoredSessionId !== activeSessionId) {
          void loadSession(restoredSessionId);
        } else if (!restoredSessionId && activeSessionId) {
          currentSessionIdRef.current = null;
          useSessionStore.getState().setCurrentSessionId(null);
          useMessageStore.getState().setMessages([]);
        }
      } catch (error) {
        if (error instanceof CancelledError) return;
        console.error('Failed to fetch sessions:', error);
      }
    },
    [api, loadSession],
  );

  useEffect(() => {
    loadSessionRef.current = loadSession;
  }, [loadSession]);

  useEffect(() => {
    fetchSessionsRef.current = fetchSessions;
  }, [fetchSessions]);

  const createNewSession = useCallback(async () => {
    if (isStreaming) return;

    try {
      const session = await api.createSession(agentId);
      const newId = session.SessionId;
      if (newId) {
        useSessionStore
          .getState()
          .upsertSessions([{ SessionId: newId, UpdatedAt: new Date().toISOString() } as unknown as Session]);
        currentSessionIdRef.current = newId;
        useSessionStore.getState().setCurrentSessionId(newId);
        useMessageStore.getState().setMessages([]);
        if (isMobile) {
          useUIStore.getState().setMobileSidebarOpen(false);
          useUIStore.getState().setMobileActionsOpen(false);
        }
        void fetchSessions(agentId, newId);
      }
    } catch (error) {
      if (error instanceof CancelledError) return;
      console.error('Failed to create session:', error);
    }
  }, [agentId, api, fetchSessions, isMobile, isStreaming]);

  const deleteSession = useCallback(
    async (sessionId: string) => {
      try {
        await api.deleteSession(sessionId);
        useSessionStore.getState().removeSession(sessionId);
        if (currentSessionIdRef.current === sessionId) {
          currentSessionIdRef.current = null;
          useMessageStore.getState().setMessages([]);
          useSessionStore.getState().setCurrentSessionId(null);
          void fetchSessions(agentId);
        }
      } catch (error) {
        if (error instanceof CancelledError) return;
        console.error('Failed to delete session', error);
      }
    },
    [agentId, api, fetchSessions],
  );

  return {
    fetchSessions,
    loadSession,
    createNewSession,
    deleteSession,
    currentSessionIdRef,
    agentIdRef,
    runSubscriptionAbortRef,
  };
}

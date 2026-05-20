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
      let shouldReloadSession = false;

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
        let terminalStatusSeen = false;

        while (!terminalStatusSeen) {
          const { value, done } = await reader.read();
          if (done) break;
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
        }
      } finally {
        if (runSubscriptionAbortRef.current === controller) {
          runSubscriptionAbortRef.current = null;
        }
        useStreamingStore.getState().setStreaming(false);
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
          const activeRuns = findActiveRunIds(events);
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

import { useRef, useCallback, useEffect, useState } from 'react';
import { useStreamingStore } from '../stores/streaming.js';
import { useUIStore } from '../stores/ui.js';
import { useSessionStore } from '../stores/session.js';
import { useMessageStore } from '../stores/message.js';
import type { RuntimeApiFormat } from '../types/api.js';
import type { UiCapabilities } from '../types/capabilities.js';
import type { ApiFacade } from '../core/api/types.js';
import { RunEngineImpl, dispatchRunEventToStores, resetDispatcherState } from '../core/run/index.js';
import type { Session } from '../components/chat/types.js';

type QueuedDraft = {
  text: string;
  attachments: File[];
};

type RunAgentContext = {
  agentId: string;
  currentSessionId: string | null;
  agentFramework: string;
  apiFormats: RuntimeApiFormat[];
  selectedModel: string;
  thinkingMode: string;
  uiCapabilities: UiCapabilities;
  isMobile: boolean;
  api: ApiFacade;
  currentSessionIdRef: React.MutableRefObject<string | null>;
  agentIdRef: React.MutableRefObject<string>;
  queuedDraftRef: React.MutableRefObject<Array<{ text: string; attachments: File[] }>>;
};

export function useRunAgent(ctx: RunAgentContext) {
  const [engine] = useState(() => new RunEngineImpl(ctx.api));
  const drainQueueRef = useRef<() => void>(() => {});

  const {
    agentId,
    apiFormats,
    agentFramework,
    selectedModel,
    thinkingMode,
    currentSessionIdRef,
    queuedDraftRef,
  } = ctx;

  useEffect(() => {
    engine.updateConfig({
      agentId,
      apiFormats: apiFormats as string[],
      agentFramework,
      selectedModel,
      thinkingMode,
    });
  }, [engine, agentId, apiFormats, agentFramework, selectedModel, thinkingMode]);

  useEffect(() => {
    const unsub = engine.subscribe(dispatchRunEventToStores);
    return unsub;
  }, [engine]);

  const enqueueDraft = useCallback((draft: QueuedDraft) => {
    queuedDraftRef.current.push(draft);
    useUIStore.getState().setQueuedDrafts((prev) => [...prev, draft]);
  }, [queuedDraftRef]);

  const startDraft = useCallback(
    (draft: QueuedDraft & { responsesInput?: unknown; previousResponseId?: string }) => {
      if (engine.stage !== 'idle') {
        return false;
      }

      resetDispatcherState();
      useUIStore.getState().setMobileActionsOpen(false);
      useStreamingStore.getState().setStreaming(true);

      const trimmedText = draft.text.trim();
      const userMessageId = String(Date.now());
      const userAttachments = draft.attachments.map((file) => ({
        name: file.name,
        url: URL.createObjectURL(file),
        type: file.type || 'application/octet-stream',
      }));

      if (trimmedText || userAttachments.length > 0) {
        useMessageStore.getState().patchMessages((prev) => [
          ...prev,
          {
            id: userMessageId,
            role: 'user',
            content: trimmedText,
            timestamp: Date.now(),
            attachments: userAttachments.length ? userAttachments : undefined,
          },
        ]);
      }

      const accepted = engine.start({
        text: draft.text,
        attachments: draft.attachments,
        responsesInput: draft.responsesInput,
        previousResponseId: draft.previousResponseId,
        sessionId: currentSessionIdRef.current,
        onSessionCreated: (sessionId: string) => {
          useSessionStore.getState().upsertSessions([{ SessionId: sessionId, UpdatedAt: new Date().toISOString() } as unknown as Session]);
          currentSessionIdRef.current = sessionId;
          useSessionStore.getState().setCurrentSessionId(sessionId);
        },
        onSessionUpsert: () => {},
        onSettled: () => drainQueueRef.current(),
      });
      if (!accepted) {
        useStreamingStore.getState().setStreaming(false);
      }
      return accepted;
    },
    [engine, currentSessionIdRef],
  );

  useEffect(() => {
    drainQueueRef.current = () => {
      const next = queuedDraftRef.current.shift();
      if (!next) {
        useUIStore.getState().setQueuedDrafts([]);
        return;
      }

      useUIStore.getState().setQueuedDrafts((prev) => prev.slice(1));
      queueMicrotask(() => {
        if (!startDraft(next)) {
          queuedDraftRef.current.unshift(next);
          useUIStore.getState().setQueuedDrafts((prev) => [next, ...prev]);
        }
      });
    };
  }, [queuedDraftRef, startDraft]);

  const submitDraft = useCallback(
    async (
      draftText: string,
      draftAttachments: File[],
      responsesInput?: unknown,
      previousResponseId?: string,
    ) => {
      const draft = {
        text: draftText,
        attachments: draftAttachments,
        responsesInput,
        previousResponseId,
      };

      if (engine.stage !== 'idle' || useStreamingStore.getState().isStreaming) {
        if (responsesInput === undefined) {
          enqueueDraft({ text: draftText, attachments: draftAttachments });
        }
        return;
      }

      if (!startDraft(draft) && responsesInput === undefined) {
        enqueueDraft({ text: draftText, attachments: draftAttachments });
      }
    },
    [engine, enqueueDraft, startDraft],
  );

  const stopGeneration = useCallback(() => {
    engine.stop();
  }, [engine]);

  const resetCompaction = useCallback(() => {
    resetDispatcherState();
  }, []);

  return { submitDraft, stopGeneration, resetCompaction };
}

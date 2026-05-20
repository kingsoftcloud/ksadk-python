import { useRef, useCallback, useEffect } from 'react';
import { useStreamingStore } from '../stores/streaming.js';
import { useUIStore } from '../stores/ui.js';
import { useSessionStore } from '../stores/session.js';
import { useMessageStore } from '../stores/message.js';
import type { RuntimeApiFormat } from '../types/api.js';
import type { UiCapabilities } from '../types/capabilities.js';
import type { ApiFacade } from '../core/api/types.js';
import type { RuntimeTransport } from '../core/transport/types.js';
import { RunEngineImpl, dispatchRunEventToStores, resetDispatcherState } from '../core/run/index.js';
import { SsePostTransport, SseGetTransport } from '../core/transport/index.js';
import type { Message, Session } from '../components/chat/types.js';

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
  const engineRef = useRef<RunEngineImpl | null>(null);

  if (!engineRef.current) {
    engineRef.current = new RunEngineImpl(
      ctx.api,
      ctx.agentId,
      ctx.apiFormats as string[],
      ctx.agentFramework,
      ctx.selectedModel,
      ctx.thinkingMode,
    );
  }

  useEffect(() => {
    const unsub = engineRef.current!.subscribe(dispatchRunEventToStores);
    return unsub;
  }, []);

  const submitDraft = useCallback(
    async (
      draftText: string,
      draftAttachments: File[],
      responsesInput?: unknown,
      previousResponseId?: string,
    ) => {
      resetDispatcherState();
      useUIStore.getState().setMobileActionsOpen(false);
      useStreamingStore.getState().setStreaming(true);

      // Add user message to store immediately
      const userMessageId = String(Date.now());
      const userAttachments = draftAttachments.map((file) => ({
        name: file.name,
        url: URL.createObjectURL(file),
        type: file.type || 'application/octet-stream',
      }));

      useMessageStore.getState().patchMessages((prev) => [
        ...prev,
        {
          id: userMessageId,
          role: 'user',
          content: draftText.trim(),
          timestamp: Date.now(),
          attachments: userAttachments.length ? userAttachments : undefined,
        },
      ]);

      engineRef.current!.start({
        text: draftText,
        attachments: draftAttachments,
        responsesInput,
        previousResponseId,
        sessionId: ctx.currentSessionIdRef.current,
        onSessionCreated: (sessionId: string) => {
          useSessionStore.getState().upsertSessions([{ SessionId: sessionId, UpdatedAt: new Date().toISOString() } as unknown as Session]);
          ctx.currentSessionIdRef.current = sessionId;
          useSessionStore.getState().setCurrentSessionId(sessionId);
        },
        onSessionUpsert: () => {},
      });
    },
    [ctx],
  );

  const stopGeneration = useCallback(() => {
    engineRef.current?.stop();
  }, []);

  const resetCompaction = useCallback(() => {
    resetDispatcherState();
  }, []);

  return { submitDraft, stopGeneration, resetCompaction };
}

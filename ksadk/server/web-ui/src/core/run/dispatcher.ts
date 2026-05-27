import type { RunEvent } from './types.js';
import { useMessageStore } from '../../stores/message.js';
import { useStreamingStore } from '../../stores/streaming.js';
import { useSessionStore } from '../../stores/session.js';
import { buildCompactionMessage } from '../../utils/session-events.js';
import type { Message } from '../../components/chat/types.js';

let assistantCreated = false;

function ensureAssistantMessage(id: string) {
  if (assistantCreated) return;
  assistantCreated = true;
  useMessageStore.getState().patchMessages((prev) => [
    ...prev,
    { id, role: 'model', content: '', timestamp: Date.now(), reasoning: '' },
  ]);
}

export function dispatchRunEventToStores(event: RunEvent) {
  if (event.sessionId && useSessionStore.getState().currentSessionId !== event.sessionId) {
    if (event.type === 'stage_changed' && (event.stage === 'completing' || event.stage === 'error' || event.stage === 'cancelled')) {
      assistantCreated = false;
    }
    return;
  }

  const ms = useMessageStore.getState();

  switch (event.type) {
    case 'activity':
      useStreamingStore.getState().updateActivity({
        sessionId: event.sessionId,
        status: event.status,
        phase: event.phase,
        detail: event.detail,
        countEvent: event.countEvent,
      });
      break;

    case 'user_message_added':
      ms.patchMessages((prev) => [
        ...prev,
        { id: event.messageId, role: 'user', content: '', timestamp: Date.now() },
      ]);
      break;

    case 'assistant_message_created':
      ensureAssistantMessage(event.messageId);
      break;

    case 'text_delta':
      ensureAssistantMessage(event.messageId);
      ms.patchMessages((prev) =>
        prev.map((msg) =>
          msg.id === event.messageId
            ? { ...msg, content: msg.content + event.delta }
            : msg,
        ),
      );
      break;

    case 'text_final':
      ensureAssistantMessage(event.messageId);
      ms.patchMessages((prev) =>
        prev.map((msg) =>
          msg.id === event.messageId
            ? { ...msg, content: event.text }
            : msg,
        ),
      );
      break;

    case 'reasoning_delta':
      ensureAssistantMessage(event.messageId);
      ms.patchMessages((prev) =>
        prev.map((msg) =>
          msg.id === event.messageId
            ? { ...msg, reasoning: (msg.reasoning || '') + event.delta }
            : msg,
        ),
      );
      break;

    case 'tool_upsert': {
      ensureAssistantMessage(event.messageId);
      ms.patchMessages((prev) =>
        prev.map((msg) => {
          if (msg.id !== event.messageId) return msg;
          return {
            ...msg,
            tools: {
              ...(msg.tools || {}),
              [event.name]: {
                ...(msg.tools?.[event.name] || { name: event.name, args: '' }),
                name: event.name,
                args: event.args,
                status: event.status as NonNullable<Message['tools']>[string]['status'],
                ...(event.extra || {}),
                ...(event.extra?.approvalRequestId ? { approvalStatus: 'pending' as const } : {}),
              },
            },
          };
        }),
      );
      break;
    }

    case 'tool_result':
      ensureAssistantMessage(event.messageId);
      ms.patchMessages((prev) =>
        prev.map((msg) => {
          if (msg.id !== event.messageId) return msg;
          return {
            ...msg,
            tools: {
              ...(msg.tools || {}),
              [event.name]: {
                ...(msg.tools?.[event.name] || { name: event.name, args: '' }),
                output: event.output,
                status: 'completed',
              },
            },
          };
        }),
      );
      break;

    case 'system_message':
      ms.patchMessages((prev) => [
        ...prev,
        {
          id: String(Date.now() + Math.random()),
          role: 'system',
          content: event.content,
          timestamp: Date.now(),
        },
      ]);
      break;

    case 'compaction': {
      const compactionId = `compaction-${Date.now()}`;
      const status: Message['status'] =
        event.phase === 'start' ? 'running'
          : event.phase === 'failed' ? 'failed'
          : 'completed';
      const compactionMsg = buildCompactionMessage({
        id: compactionId,
        timestamp: Date.now(),
        status,
        trigger: event.trigger,
        compactedUntilSeqId: event.compactedUntilSeqId,
      });
      ms.patchMessages((prev) => {
        const existingIndex = prev.findIndex((m) => m.id === compactionId);
        if (existingIndex < 0) return [...prev, compactionMsg as Message];
        return prev.map((m) => (m.id === compactionId ? { ...m, ...compactionMsg } : m));
      });
      break;
    }

    case 'stage_changed':
      if (event.stage === 'streaming' || event.stage === 'connecting') {
        useStreamingStore.getState().setSessionStreaming(event.sessionId, true);
      } else if (event.stage === 'completing' || event.stage === 'error' || event.stage === 'cancelled') {
        useStreamingStore.getState().setSessionStreaming(event.sessionId, false);
        assistantCreated = false;
      }
      break;

    case 'stream_ended':
      useStreamingStore.getState().setSessionStreaming(event.sessionId, false);
      globalThis.setTimeout(() => {
        const state = useStreamingStore.getState();
        const activity = state.getSessionActivity(event.sessionId);
        if (activity?.status === 'completed') {
          state.clearSessionActivity(event.sessionId);
        }
      }, 2400);
      assistantCreated = false;
      break;

    case 'error':
      useStreamingStore.getState().setSessionStreaming(event.sessionId, false);
      useStreamingStore.getState().updateActivity({
        sessionId: event.sessionId,
        status: 'failed',
        phase: '连接断开或生成出错',
        countEvent: false,
      });
      ms.patchMessages((prev) => [
        ...prev,
        {
          id: String(Date.now()),
          role: 'model',
          content: '连接断开或生成出错。',
          timestamp: Date.now(),
        },
      ]);
      assistantCreated = false;
      break;

    case 'terminal':
      // Terminal is informational; state already reset by stage_changed or stream_ended
      break;
  }
}

export function resetDispatcherState() {
  assistantCreated = false;
}

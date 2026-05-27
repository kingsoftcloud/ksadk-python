import type { RuntimeApiFormat } from '../../types/api.js';

export type RunStage =
  | 'idle' | 'creating-session' | 'uploading-files'
  | 'connecting' | 'streaming' | 'stopping'
  | 'completing' | 'recovering' | 'error'
  | 'cancelled';

export type RunEvent =
  | { type: 'stage_changed'; stage: RunStage; sessionId?: string | null }
  | {
      type: 'activity';
      sessionId?: string | null;
      phase: string;
      status?: 'connecting' | 'running' | 'waiting' | 'stopped' | 'completed' | 'failed';
      detail?: string;
      countEvent?: boolean;
    }
  | { type: 'user_message_added'; messageId: string; sessionId?: string | null }
  | { type: 'assistant_message_created'; messageId: string; sessionId?: string | null }
  | { type: 'text_delta'; messageId: string; delta: string; sessionId?: string | null }
  | { type: 'text_final'; messageId: string; text: string; sessionId?: string | null }
  | { type: 'reasoning_delta'; messageId: string; delta: string; sessionId?: string | null }
  | { type: 'tool_upsert'; messageId: string; name: string; args: string; status: string; extra?: Record<string, unknown>; sessionId?: string | null }
  | { type: 'tool_result'; messageId: string; name: string; output: string; sessionId?: string | null }
  | { type: 'compaction'; phase: string; trigger?: string; compactedUntilSeqId?: number; sessionId?: string | null }
  | { type: 'system_message'; content: string; sessionId?: string | null }
  | { type: 'stream_ended'; sessionId?: string | null }
  | { type: 'error'; error: Error; sessionId?: string | null }
  | { type: 'terminal'; status: string; sessionId?: string | null }
  | { type: 'stream_event'; event: import('../../types/session-events.js').SessionEventRecord; sessionId?: string | null };

export type RunEngineConfig = {
  agentId: string;
  apiFormats: RuntimeApiFormat[];
  agentFramework: string;
  selectedModel: string;
  thinkingMode: string;
};

export interface RunEngine {
  updateConfig(config: RunEngineConfig): void;
  start(draft: {
    text: string;
    attachments: File[];
    responsesInput?: unknown;
    previousResponseId?: string;
    sessionId?: string | null;
    onSessionCreated?: (sessionId: string) => void;
    onSessionUpsert?: (sessionId: string) => void;
    onSettled?: (sessionId: string | null) => void;
  }): boolean;
  disconnect(): void;
  stop(): void;
  cancelRemote(invocationId: string): Promise<void>;
  resumeRun(params: {
    sessionId: string;
    invocationId: string;
    afterSeqId: number;
    onSessionReloadNeeded?: () => void;
  }): void;
  readonly stage: RunStage;
  subscribe(listener: (event: RunEvent) => void): () => void;
}

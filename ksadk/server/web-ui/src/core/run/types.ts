export type RunStage =
  | 'idle' | 'creating-session' | 'uploading-files'
  | 'connecting' | 'streaming' | 'stopping'
  | 'completing' | 'failed' | 'cancelled';

export type RunEvent =
  | { type: 'stage_changed'; stage: RunStage }
  | { type: 'user_message_added'; messageId: string }
  | { type: 'assistant_message_created'; messageId: string }
  | { type: 'text_delta'; messageId: string; delta: string }
  | { type: 'text_final'; messageId: string; text: string }
  | { type: 'reasoning_delta'; messageId: string; delta: string }
  | { type: 'tool_upsert'; messageId: string; name: string; args: string; status: string; extra?: Record<string, unknown> }
  | { type: 'tool_result'; messageId: string; name: string; output: string }
  | { type: 'compaction'; phase: string; trigger?: string; compactedUntilSeqId?: number }
  | { type: 'system_message'; content: string }
  | { type: 'stream_ended' }
  | { type: 'error'; error: Error }
  | { type: 'terminal'; status: string }
  | { type: 'stream_event'; event: import('../../types/session-events.js').SessionEventRecord };

export interface RunEngine {
  start(draft: {
    text: string;
    attachments: File[];
    responsesInput?: unknown;
    previousResponseId?: string;
    sessionId?: string | null;
    onSessionCreated?: (sessionId: string) => void;
    onSessionUpsert?: (sessionId: string) => void;
  }): void;
  stop(): void;
  resumeRun(params: {
    sessionId: string;
    invocationId: string;
    afterSeqId: number;
    onSessionReloadNeeded?: () => void;
  }): void;
  readonly stage: RunStage;
  subscribe(listener: (event: RunEvent) => void): () => void;
}

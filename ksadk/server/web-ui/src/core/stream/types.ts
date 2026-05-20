export type StreamAction =
  | { type: 'text_delta'; text: string }
  | { type: 'text_final'; text: string }
  | { type: 'reasoning_delta'; text: string }
  | { type: 'tool_upsert'; name: string; args: string; status: 'running' | 'completed' | 'error' | 'paused'; extra?: Record<string, unknown> }
  | { type: 'tool_result'; name: string; output: string }
  | { type: 'approval_request'; approvalRequestId: string; previousResponseId?: string }
  | { type: 'compaction'; phase: 'start' | 'done' | 'failed'; trigger?: string; compactedUntilSeqId?: number }
  | { type: 'incomplete' }
  | { type: 'failed'; message: string }
  | { type: 'terminal'; status: string };

export interface StreamProtocol {
  createState(): Record<string, unknown>;
  parse(event: import('../transport/types.js').TransportEvent, state: Record<string, unknown>): StreamAction[];
  readonly id: string;
}

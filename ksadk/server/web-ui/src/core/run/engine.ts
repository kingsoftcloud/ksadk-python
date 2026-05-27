import type { RunEngine, RunStage, RunEvent, RunEngineConfig } from './types.js';
import type { ApiFacade } from '../api/types.js';
import type { StreamAction } from '../stream/types.js';
import { createProtocol } from '../stream/index.js';
import { shouldStopReadingRunStream } from '../../utils/stream-control.js';
import { parseSseChunk, splitSseBuffer } from '../transport/sse-parser.js';
import type { SessionEventRecord } from '../../types/session-events.js';
import { getErrorMessage } from '../../utils/error.js';
import { buildModelOptionsFromThinkingMode, normalizeThinkingMode } from '../../utils/model-options.js';
import { resolveRunAgentApiFormat } from '../../utils/layout-constants.js';
import { useStreamingStore } from '../../stores/streaming.js';
import type { StreamProtocol } from '../stream/types.js';
import type { RuntimeApiFormat } from '../../types/api.js';

type StreamConsumeResult = {
  receivedData: boolean;
  terminalStatus?: string;
};

function activityForTransportEvent(eventName: string, data: unknown): { phase: string; status?: 'running' | 'waiting' | 'completed' | 'failed'; detail?: string } | null {
  const eventType = String((data as Record<string, unknown> | null)?.type || eventName || '').trim();
  if (eventType === 'response.created') {
    return { phase: '运行已创建', status: 'running' };
  }
  if (eventType === 'response.in_progress') {
    return { phase: '等待运行时输出', status: 'waiting' };
  }
  if (eventType === 'response.output_item.added') {
    const item = (data as { item?: { type?: string; name?: string } } | null)?.item;
    if (item?.type === 'function_call') {
      return { phase: `调用工具 ${item.name || 'tool'}`, status: 'running' };
    }
    if (String(item?.type || '').includes('reasoning')) {
      return { phase: '生成思考过程', status: 'running' };
    }
    return { phase: '生成回复内容', status: 'running' };
  }
  if (eventType.includes('reasoning')) {
    return { phase: '生成思考过程', status: 'running' };
  }
  if (eventType.includes('function_call') || eventType === 'response.tool_call') {
    return { phase: '调用工具', status: 'running' };
  }
  if (eventType.includes('tool_result')) {
    return { phase: '收到工具结果', status: 'running' };
  }
  if (eventType.includes('output_text') || eventType.includes('content_part')) {
    return { phase: '生成回复内容', status: 'running' };
  }
  if (eventType === 'response.completed') {
    return { phase: '运行完成', status: 'completed' };
  }
  if (eventType === 'response.failed') {
    return { phase: '运行失败', status: 'failed' };
  }
  if (eventType === 'response.incomplete') {
    return { phase: '运行中断', status: 'failed' };
  }
  return null;
}

const VALID_TRANSITIONS: Record<RunStage, RunStage[]> = {
  idle: ['creating-session', 'uploading-files', 'connecting', 'error'],
  'creating-session': ['uploading-files', 'connecting', 'error', 'idle'],
  'uploading-files': ['connecting', 'error', 'idle'],
  connecting: ['streaming', 'error', 'idle'],
  streaming: ['completing', 'stopping', 'recovering', 'error'],
  stopping: ['cancelled', 'idle'],
  completing: ['idle'],
  recovering: ['streaming', 'error', 'idle'],
  error: ['connecting', 'idle'],
  cancelled: ['idle'],
};

export class RunEngineImpl implements RunEngine {
  private _stage: RunStage = 'idle';
  private listeners = new Set<(event: RunEvent) => void>();
  private abortController: AbortController | null = null;
  private activeCompactionId: string | null = null;
  private activeSessionId: string | null = null;
  private api: ApiFacade;
  private config: RunEngineConfig = {
    agentId: 'default-agent',
    apiFormats: ['responses'],
    agentFramework: '',
    selectedModel: '',
    thinkingMode: 'auto',
  };

  constructor(api: ApiFacade) {
    this.api = api;
  }

  get stage() { return this._stage; }

  updateConfig(config: RunEngineConfig): void {
    this.config = {
      ...config,
      apiFormats: [...config.apiFormats],
    };
  }

  private emit(event: RunEvent) {
    const scopedEvent = 'sessionId' in event
      ? event
      : { ...event, sessionId: this.activeSessionId };
    for (const listener of this.listeners) {
      listener(scopedEvent as RunEvent);
    }
  }

  private setStage(stage: RunStage) {
    const allowed = VALID_TRANSITIONS[this._stage];
    if (allowed && !allowed.includes(stage)) {
      console.warn(`[RunEngine] Invalid transition: ${this._stage} → ${stage}`);
    }
    this._stage = stage;
    this.emit({ type: 'stage_changed', stage });
  }

  subscribe(listener: (event: RunEvent) => void): () => void {
    this.listeners.add(listener);
    return () => { this.listeners.delete(listener); };
  }

  start(draft: {
    text: string;
    attachments: File[];
    responsesInput?: unknown;
    previousResponseId?: string;
    sessionId?: string | null;
    onSessionCreated?: (sessionId: string) => void;
    onSessionUpsert?: (sessionId: string) => void;
    onSettled?: (sessionId: string | null) => void;
  }): boolean {
    if (this._stage !== 'idle') return false;

    this.abortController = new AbortController();
    const isResponsesResume = draft.responsesInput !== undefined;

    (async () => {
      let sessionId: string | null = draft.sessionId || null;
      try {
        let retriedWithNewSession = false;

        if (!sessionId) {
          sessionId = await this.createSession(draft);
        }

        if (!sessionId) {
          sessionId = `default-session-${Date.now()}`;
        }
        this.activeSessionId = sessionId;

        const fileParts = await this.uploadFiles(draft, isResponsesResume);

        this.setStage('connecting');
        this.emit({ type: 'activity', phase: '连接运行时', status: 'connecting', countEvent: false });
        const apiFormat = isResponsesResume
          ? 'responses'
          : resolveRunAgentApiFormat({ agentFramework: this.config.agentFramework, apiFormats: this.config.apiFormats });

        const protocol = createProtocol(apiFormat);
        const protocolState = protocol.createState();

        const body = this.buildRequestBody(sessionId, apiFormat, isResponsesResume, draft, fileParts);

        const stream = await this.api.runAgent(body, { signal: this.abortController?.signal });
        this.setStage('streaming');
        this.emit({ type: 'activity', phase: '等待首个输出', status: 'waiting', countEvent: false });

        const assistantMessageId = `msg-${Date.now()}`;

        const streamResult = await this.consumeStream(stream, protocol, protocolState, assistantMessageId);

        if (!streamResult.receivedData && !retriedWithNewSession) {
          retriedWithNewSession = true;
          sessionId = await this.createSession(draft);
          if (sessionId) {
            this.activeSessionId = sessionId;
            body.SessionId = sessionId;
            this.setStage('connecting');
            this.emit({ type: 'activity', phase: '重建会话后重新连接', status: 'connecting', countEvent: false });
            const retryStream = await this.api.runAgent(body, { signal: this.abortController?.signal });
            this.setStage('streaming');
            this.emit({ type: 'activity', phase: '等待首个输出', status: 'waiting', countEvent: false });
            const retryMsgId = `msg-${Date.now()}`;
            const retryResult = await this.consumeStream(retryStream, protocol, protocolState, retryMsgId);
            streamResult.terminalStatus = retryResult.terminalStatus;
          }
        }

        if (streamResult.terminalStatus && streamResult.terminalStatus !== 'completed') {
          this.setStage('error');
          this.emit({
            type: 'activity',
            phase: streamResult.terminalStatus === 'incomplete' ? '运行中断' : '运行失败',
            status: 'failed',
            countEvent: false,
          });
          return;
        }

        this.setStage('completing');
        this.emit({ type: 'activity', phase: '运行完成', status: 'completed', countEvent: false });
        this.emit({ type: 'stream_ended' });
      } catch (error) {
        this.failCompaction();
        const isAbort = error instanceof DOMException && error.name === 'AbortError';
        if (!isAbort) {
          console.error('[RunEngine] start() error:', error);
          const isNetwork = error instanceof TypeError && error.message.includes('fetch');
          if (isNetwork) {
            this.setStage('recovering');
            this.emit({ type: 'activity', phase: '网络异常，尝试重连', status: 'waiting', countEvent: false });
          } else {
            this.setStage('error');
            this.emit({ type: 'error', error: error instanceof Error ? error : new Error(String(error)) });
          }
        }
      } finally {
        useStreamingStore.getState().setStreaming(false);
        this.setStage('idle');
        this.activeCompactionId = null;
        this.activeSessionId = null;
        draft.onSettled?.(sessionId);
      }
    })();
    return true;
  }

  stop(): void {
    if (this._stage === 'idle') return;
    this.setStage('stopping');
    this.abortController?.abort();
    useStreamingStore.getState().stopActivity();
    this.setStage('cancelled');
    this.emit({
      type: 'system_message',
      content: '已停止接收本次输出；如果运行时不支持取消，后台执行可能仍会继续。',
    });
  }

  disconnect(): void {
    if (this._stage === 'idle') return;
    this.abortController?.abort();
    useStreamingStore.getState().setStreaming(false);
    useStreamingStore.getState().clearActivity();
    this._stage = 'idle';
    this.activeCompactionId = null;
    this.activeSessionId = null;
  }

  async cancelRemote(invocationId: string): Promise<void> {
    try {
      await this.api.cancelRun(this.config.agentId, invocationId);
    } catch (err) {
      console.warn('[RunEngine] cancelRemote failed:', err);
    }
    this.stop();
  }

  resumeRun(params: {
    sessionId: string;
    invocationId: string;
    afterSeqId: number;
    onSessionReloadNeeded?: () => void;
  }): void {
    this.abortController?.abort();
    this.abortController = new AbortController();
    this.activeSessionId = params.sessionId;
    this.setStage('connecting');
    this.emit({ type: 'activity', phase: '恢复运行事件订阅', status: 'connecting', countEvent: false });

    (async () => {
      try {
        const stream = await this.api.subscribeRunEvents(
          {
            sessionId: params.sessionId,
            invocationId: params.invocationId,
            afterSeqId: params.afterSeqId,
          },
          { signal: this.abortController?.signal },
        );
        this.setStage('streaming');
        this.emit({ type: 'activity', phase: '等待恢复事件', status: 'waiting', countEvent: false });

        const reader = stream.getReader();
        const decoder = new TextDecoder();
        let buffer = '';

        while (true) {
          const { value, done } = await reader.read();
          if (done) break;

          buffer += decoder.decode(value, { stream: true });
          const { chunks, remainder } = splitSseBuffer(buffer);
          buffer = remainder;

          for (const chunk of chunks) {
            if (!chunk.trim()) continue;
            const events = parseSseChunk(chunk);
            for (const event of events) {
              if (event.eventName === '__done__') continue;
              this.emit({ type: 'activity', phase: '收到恢复事件', status: 'running' });
              this.emit({ type: 'stream_event', event: event.data as SessionEventRecord });
            }
          }
        }

        this.setStage('completing');
        this.emit({ type: 'activity', phase: '恢复订阅结束', status: 'completed', countEvent: false });
        this.emit({ type: 'stream_ended' });
        params.onSessionReloadNeeded?.();
      } catch (error) {
        const isAbort = error instanceof DOMException && error.name === 'AbortError';
        if (!isAbort) {
          console.error('Failed to subscribe to run events:', error);
        }
      } finally {
        useStreamingStore.getState().setStreaming(false);
        this.setStage('idle');
        this.activeSessionId = null;
      }
    })();
  }

  private async createSession(draft: {
    onSessionCreated?: (sessionId: string) => void;
    onSessionUpsert?: (sessionId: string) => void;
  }): Promise<string | null> {
    this.setStage('creating-session');
    try {
      const session = await this.api.createSession(this.config.agentId, { signal: this.abortController?.signal });
      const sessionId = session.SessionId || null;
      if (sessionId) {
        draft.onSessionCreated?.(sessionId);
        draft.onSessionUpsert?.(sessionId);
      }
      return sessionId;
    } catch (error) {
      if (!(error instanceof DOMException && error.name === 'AbortError')) {
        console.error('Failed to create session:', error);
      }
      return null;
    }
  }

  private async uploadFiles(draft: { attachments: File[] }, isResponsesResume: boolean): Promise<Array<Record<string, unknown>>> {
    const fileParts: Array<Record<string, unknown>> = [];
    if (isResponsesResume || draft.attachments.length === 0) return fileParts;

    this.setStage('uploading-files');
    for (const file of draft.attachments) {
      if (file.size > 100 * 1024 * 1024) {
        this.emit({ type: 'system_message', content: `【系统提示】文件 ${file.name} 超过 100MB 限制，未发送。` });
        continue;
      }
      if (file.type.startsWith('image/')) {
        try {
          fileParts.push({
            type: 'input_image',
            image_url: await this.imageFileToDataUrl(file),
          });
        } catch (error) {
          this.emit({ type: 'system_message', content: `【系统提示】图片 ${file.name} 读取失败，原因: ${getErrorMessage(error)}` });
        }
        continue;
      }
      const formData = new FormData();
      formData.append('file', file);
      try {
        const uploadData = await this.api.uploadFile(formData, { signal: this.abortController?.signal });
        if (uploadData?.FileData?.fileUri) {
          fileParts.push({
            type: 'input_file',
            filename: uploadData.FileData.displayName || file.name,
            file_url: uploadData.FileData.fileUri,
          });
        }
      } catch (error) {
        this.emit({ type: 'system_message', content: `【系统提示】文件 ${file.name} 上传失败，原因: ${getErrorMessage(error)}` });
      }
    }
    return fileParts;
  }

  private async imageFileToDataUrl(file: File): Promise<string> {
    const bytes = new Uint8Array(await file.arrayBuffer());
    let binary = '';
    const chunkSize = 0x8000;
    for (let offset = 0; offset < bytes.length; offset += chunkSize) {
      binary += String.fromCharCode(...bytes.subarray(offset, offset + chunkSize));
    }
    return `data:${file.type || 'application/octet-stream'};base64,${btoa(binary)}`;
  }

  private buildRequestBody(
    sessionId: string,
    apiFormat: RuntimeApiFormat,
    isResponsesResume: boolean,
    draft: { text: string; responsesInput?: unknown; previousResponseId?: string },
    fileParts: Array<Record<string, unknown>>,
  ): Record<string, unknown> {
    const body: Record<string, unknown> = {
      AgentId: this.config.agentId,
      SessionId: sessionId,
      Stream: true,
      ApiFormat: apiFormat,
      Model: this.config.selectedModel || undefined,
      ModelOptions: buildModelOptionsFromThinkingMode(normalizeThinkingMode(this.config.thinkingMode)),
    };

    if (!isResponsesResume) {
      const parts: Array<Record<string, unknown>> = [{ type: 'input_text', text: draft.text.trim() }];
      parts.push(...fileParts);
      if (apiFormat === 'responses') {
        body.ResponsesInput = [{ role: 'user', content: parts }];
      }
      body.Messages = [{ role: 'user', content: parts }];
    } else {
      body.ResponsesInput = draft.responsesInput;
      body.PreviousResponseId = draft.previousResponseId;
    }
    return body;
  }

  private async consumeStream(
    stream: ReadableStream<Uint8Array>,
    protocol: StreamProtocol,
    protocolState: Record<string, unknown>,
    messageId: string,
  ): Promise<StreamConsumeResult> {
    const reader = stream.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    let receivedData = false;
    let messageCreated = false;

    try {
      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        receivedData = true;

        if (!messageCreated) {
          messageCreated = true;
          this.emit({ type: 'assistant_message_created', messageId });
        }

        buffer += decoder.decode(value, { stream: true });
        const { chunks, remainder } = splitSseBuffer(buffer);
        buffer = remainder;

        for (const chunk of chunks) {
          if (!chunk.trim()) continue;

          if (this.isCompactionChunk(chunk)) {
            const events = parseSseChunk(chunk);
            for (const ev of events) {
              if (ev.eventName.startsWith('response.compaction')) {
                this.upsertCompactionMessage({ ...(ev.data as Record<string, unknown>), phase: ev.eventName.split('.').pop() });
              }
            }
            continue;
          }

          const events = parseSseChunk(chunk);
          let shouldStop = false;
          let terminalStatus: string | undefined;

          for (const event of events) {
            if (event.eventName === '__done__') {
              shouldStop = true;
              continue;
            }

            const activity = activityForTransportEvent(event.eventName, event.data);
            if (activity) {
              this.emit({ type: 'activity', ...activity });
            }

            const actions = protocol.parse(event, protocolState);
            for (const action of actions) {
              this.dispatchAction(action, messageId);
            }

            if (shouldStopReadingRunStream(actions as Array<{ type: string; status?: string }>)) {
              shouldStop = true;
              const terminalAction = actions.find((action) => action.type === 'terminal');
              terminalStatus = terminalAction && 'status' in terminalAction
                ? String(terminalAction.status || '')
                : undefined;
            }
          }

          if (shouldStop) {
            reader.cancel().catch(() => {});
            return { receivedData, terminalStatus };
          }
        }
      }
    } catch (error) {
      if (!(error instanceof DOMException && error.name === 'AbortError')) {
        throw error;
      }
    }

    return { receivedData };
  }

  private isCompactionChunk(chunk: string): boolean {
    return chunk.includes('response.compaction.start') || chunk.includes('response.compaction.done') || chunk.includes('response.compaction.failed');
  }

  private dispatchAction(action: StreamAction, messageId: string) {
    switch (action.type) {
      case 'text_delta':
        this.emit({ type: 'text_delta', messageId, delta: action.text });
        break;
      case 'text_final':
        this.emit({ type: 'text_final', messageId, text: action.text });
        break;
      case 'reasoning_delta':
        this.emit({ type: 'reasoning_delta', messageId, delta: action.text });
        break;
      case 'tool_upsert':
        this.emit({ type: 'tool_upsert', messageId, name: action.name, args: action.args, status: action.status, extra: action.extra });
        break;
      case 'tool_result':
        this.emit({ type: 'tool_result', messageId, name: action.name, output: action.output });
        break;
      case 'approval_request':
        this.emit({ type: 'system_message', content: '本次运行需要人工审批后才能继续。' });
        break;
      case 'incomplete':
        this.emit({ type: 'system_message', content: '本次运行已中断，需要人工确认后继续。' });
        break;
      case 'failed':
        this.emit({ type: 'text_final', messageId, text: `生成失败：${action.message}` });
        break;
      case 'terminal':
        this.emit({ type: 'terminal', status: action.status });
        break;
      case 'compaction':
        this.emit({ type: 'compaction', phase: action.phase, trigger: action.trigger, compactedUntilSeqId: action.compactedUntilSeqId });
        break;
    }
  }

  private upsertCompactionMessage(payload: Record<string, unknown>) {
    const eventPhase =
      typeof payload.eventName === 'string'
        ? payload.eventName.split('.').pop()
        : undefined;
    const phase = String(payload.phase || eventPhase || 'start');
    this.emit({
      type: 'compaction',
      phase,
      trigger: payload.trigger ? String(payload.trigger) : undefined,
      compactedUntilSeqId: payload.compacted_until_seq_id ? Number(payload.compacted_until_seq_id) : undefined,
    });
    if (phase !== 'start') {
      this.activeCompactionId = null;
    }
  }

  private failCompaction() {
    if (this.activeCompactionId) {
      this.emit({ type: 'compaction', phase: 'failed' });
      this.activeCompactionId = null;
    }
  }
}

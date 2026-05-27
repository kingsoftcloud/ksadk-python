import { afterEach, describe, expect, it, vi } from 'vitest';
import { RunEngineImpl } from '../core/run/engine.js';
import type { ApiFacade } from '../core/api/types.js';
import { useStreamingStore } from '../stores/streaming.js';
import { useMessageStore } from '../stores/message.js';
import { useSessionStore } from '../stores/session.js';
import { dispatchRunEventToStores, resetDispatcherState } from '../core/run/dispatcher.js';

function createApiFacade(calls: Record<string, unknown>[]): ApiFacade {
  return {
    async listSessions() { return []; },
    async createSession() { return { SessionId: 'session-1' }; },
    async deleteSession() {},
    async listSessionEvents() { return { Events: [] }; },
    async runAgent(body) {
      calls.push(body);
      return new ReadableStream<Uint8Array>({
        start(controller) {
          controller.enqueue(new TextEncoder().encode('data: [DONE]\n\n'));
          controller.close();
        },
      });
    },
    async subscribeRunEvents() {
      return new ReadableStream<Uint8Array>();
    },
    async getResponseFeedback() { return null; },
    async upsertResponseFeedback() { return null; },
    async deleteResponseFeedback() {},
    async listWorkspaceFiles() { return {}; },
    async addWorkspaceFile() { return {}; },
    async deleteWorkspaceFile() {},
    async getWorkspaceFileContent() { return ''; },
    async listAgentModels() { return {}; },
    async getAgentUiBootstrap() { return {}; },
    async uploadFile() {
      return { FileData: { fileUri: 'file-1', displayName: 'file.txt', mimeType: 'text/plain' } };
    },
  };
}

async function waitForCalls(calls: Record<string, unknown>[], count = 1) {
  for (let i = 0; i < 20; i += 1) {
    if (calls.length >= count) return;
    await new Promise((resolve) => setTimeout(resolve, 0));
  }
  throw new Error(`expected ${count} runAgent call(s), got ${calls.length}`);
}

describe('RunEngineImpl', () => {
  afterEach(() => {
    useStreamingStore.getState().resetRun();
    useMessageStore.getState().setMessages([]);
    useSessionStore.getState().setCurrentSessionId(null);
    resetDispatcherState();
  });

  it('uses the latest runtime config when starting a run', async () => {
    const calls: Record<string, unknown>[] = [];
    const engine = new RunEngineImpl(createApiFacade(calls));

    engine.updateConfig({
      agentId: 'agent-live',
      apiFormats: ['responses'],
      agentFramework: 'langgraph',
      selectedModel: 'model-live',
      thinkingMode: 'disabled',
    });

    engine.start({
      text: 'hello',
      attachments: [],
      sessionId: 'session-live',
    });

    await new Promise((resolve) => setTimeout(resolve, 0));

    expect(calls[0]).toMatchObject({
      AgentId: 'agent-live',
      SessionId: 'session-live',
      Model: 'model-live',
      ModelOptions: { thinking: { type: 'disabled' } },
    });
  });

  it('sends ordinary hosted chat runs as Responses input while keeping legacy Messages', async () => {
    const calls: Record<string, unknown>[] = [];
    const engine = new RunEngineImpl(createApiFacade(calls));

    engine.updateConfig({
      agentId: 'agent-live',
      apiFormats: ['responses', 'chat_completions'],
      agentFramework: 'langgraph',
      selectedModel: '',
      thinkingMode: 'auto',
    });

    engine.start({
      text: 'hello responses',
      attachments: [],
      sessionId: 'session-live',
    });

    await waitForCalls(calls);

    const expectedContent = [{ type: 'input_text', text: 'hello responses' }];
    expect(calls[0]).toMatchObject({
      ApiFormat: 'responses',
      ResponsesInput: [{ role: 'user', content: expectedContent }],
      Messages: [{ role: 'user', content: expectedContent }],
    });
  });

  it('sends uploaded attachments as Responses input_file references', async () => {
    const calls: Record<string, unknown>[] = [];
    const engine = new RunEngineImpl(createApiFacade(calls));

    engine.updateConfig({
      agentId: 'agent-live',
      apiFormats: ['responses'],
      agentFramework: 'langgraph',
      selectedModel: '',
      thinkingMode: 'auto',
    });

    engine.start({
      text: 'read this',
      attachments: [new File(['hello'], 'local.txt', { type: 'text/plain' })],
      sessionId: 'session-live',
    });

    await waitForCalls(calls);

    const expectedContent = [
      { type: 'input_text', text: 'read this' },
      { type: 'input_file', filename: 'file.txt', file_url: 'file-1' },
    ];
    expect(calls[0]).toMatchObject({
      ApiFormat: 'responses',
      ResponsesInput: [{ role: 'user', content: expectedContent }],
      Messages: [{ role: 'user', content: expectedContent }],
    });
  });

  it('sends image attachments as Responses input_image data URLs', async () => {
    const calls: Record<string, unknown>[] = [];
    const engine = new RunEngineImpl(createApiFacade(calls));

    engine.updateConfig({
      agentId: 'agent-live',
      apiFormats: ['responses'],
      agentFramework: 'langgraph',
      selectedModel: '',
      thinkingMode: 'auto',
    });

    const image = new File([new Uint8Array([0x89, 0x50, 0x4e, 0x47])], 'sample.png', {
      type: 'image/png',
    });
    const imageBase64 = Buffer.from(await image.arrayBuffer()).toString('base64');

    engine.start({
      text: 'look at this',
      attachments: [image],
      sessionId: 'session-live',
    });

    await waitForCalls(calls);

    const expectedContent = [
      { type: 'input_text', text: 'look at this' },
      { type: 'input_image', image_url: `data:image/png;base64,${imageBase64}` },
    ];
    expect(calls[0]).toMatchObject({
      ApiFormat: 'responses',
      ResponsesInput: [{ role: 'user', content: expectedContent }],
      Messages: [{ role: 'user', content: expectedContent }],
    });
  });

  it('does not warn when uploading attachments for an existing session', async () => {
    const calls: Record<string, unknown>[] = [];
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {});
    const engine = new RunEngineImpl(createApiFacade(calls));

    engine.updateConfig({
      agentId: 'agent-live',
      apiFormats: ['responses'],
      agentFramework: 'langgraph',
      selectedModel: '',
      thinkingMode: 'auto',
    });

    engine.start({
      text: 'look at this',
      attachments: [new File([new Uint8Array([0x89, 0x50, 0x4e, 0x47])], 'sample.png', { type: 'image/png' })],
      sessionId: 'session-live',
    });

    await waitForCalls(calls);

    expect(warnSpy).not.toHaveBeenCalledWith('[RunEngine] Invalid transition: idle → uploading-files');
    warnSpy.mockRestore();
  });

  it('creates a session and continues to run the first image message', async () => {
    const calls: Record<string, unknown>[] = [];
    const engine = new RunEngineImpl(createApiFacade(calls));

    engine.updateConfig({
      agentId: 'agent-live',
      apiFormats: ['responses'],
      agentFramework: 'langgraph',
      selectedModel: '',
      thinkingMode: 'auto',
    });

    const image = new File([new Uint8Array([0x89, 0x50, 0x4e, 0x47])], 'first.png', {
      type: 'image/png',
    });
    const imageBase64 = Buffer.from(await image.arrayBuffer()).toString('base64');
    const createdSessions: string[] = [];

    engine.start({
      text: 'first image turn',
      attachments: [image],
      sessionId: null,
      onSessionCreated: (sessionId) => {
        createdSessions.push(sessionId);
      },
    });

    await waitForCalls(calls);

    expect(createdSessions).toEqual(['session-1']);
    expect(calls[0]).toMatchObject({
      SessionId: 'session-1',
      ApiFormat: 'responses',
      ResponsesInput: [
        {
          role: 'user',
          content: [
            { type: 'input_text', text: 'first image turn' },
            { type: 'input_image', image_url: `data:image/png;base64,${imageBase64}` },
          ],
        },
      ],
    });
  });

  it('updates run activity state when activity events are dispatched', () => {
    dispatchRunEventToStores({
      type: 'activity',
      phase: '等待首个输出',
      status: 'waiting',
      detail: '正在接收流式事件',
      countEvent: false,
    });

    const activity = useStreamingStore.getState().activity;
    expect(activity).toMatchObject({
      phase: '等待首个输出',
      status: 'waiting',
      detail: '正在接收流式事件',
    });
  });

  it('reports the settled session id after a run finishes', async () => {
    const calls: Record<string, unknown>[] = [];
    const engine = new RunEngineImpl(createApiFacade(calls));
    const settledSessionIds: Array<string | null> = [];

    engine.updateConfig({
      agentId: 'agent-live',
      apiFormats: ['responses'],
      agentFramework: 'langgraph',
      selectedModel: '',
      thinkingMode: 'auto',
    });

    engine.start({
      text: 'hello',
      attachments: [],
      sessionId: 'session-live',
      onSettled: (sessionId) => {
        settledSessionIds.push(sessionId);
      },
    });

    await waitForCalls(calls);
    for (let i = 0; i < 20 && settledSessionIds.length === 0; i += 1) {
      await new Promise((resolve) => setTimeout(resolve, 0));
    }

    expect(settledSessionIds).toEqual(['session-live']);
  });

  it('keeps response.failed as failed instead of overwriting it as completed', async () => {
    const calls: Record<string, unknown>[] = [];
    const engine = new RunEngineImpl({
      ...createApiFacade(calls),
      async runAgent(body) {
        calls.push(body);
        return new ReadableStream<Uint8Array>({
          start(controller) {
            controller.enqueue(new TextEncoder().encode(
              'event: response.failed\n'
              + 'data: {"error":{"message":"image_url is only supported by certain models"}}\n\n',
            ));
            controller.close();
          },
        });
      },
    });

    engine.updateConfig({
      agentId: 'agent-live',
      apiFormats: ['responses'],
      agentFramework: 'langgraph',
      selectedModel: 'glm-5.1',
      thinkingMode: 'auto',
    });

    engine.subscribe(dispatchRunEventToStores);
    useSessionStore.getState().setCurrentSessionId('session-live');
    engine.start({
      text: 'look at image',
      attachments: [],
      sessionId: 'session-live',
    });

    await waitForCalls(calls);
    for (let i = 0; i < 20 && engine.stage !== 'idle'; i += 1) {
      await new Promise((resolve) => setTimeout(resolve, 0));
    }

    const activity = useStreamingStore.getState().getSessionActivity('session-live');
    const messages = useMessageStore.getState().messages;
    expect(activity?.status).toBe('failed');
    expect(activity?.phase).toBe('运行失败');
    expect(messages.at(-1)?.content).toContain('image_url is only supported by certain models');
  });

  it('disconnects the frontend stream without adding a stopped system message', async () => {
    const calls: Record<string, unknown>[] = [];
    const engine = new RunEngineImpl({
      ...createApiFacade(calls),
      async runAgent(body) {
        calls.push(body);
        return new ReadableStream<Uint8Array>({
          start(controller) {
            controller.enqueue(new TextEncoder().encode('data: {"type":"response.in_progress"}\n\n'));
          },
        });
      },
    });

    engine.updateConfig({
      agentId: 'agent-live',
      apiFormats: ['responses'],
      agentFramework: 'langgraph',
      selectedModel: '',
      thinkingMode: 'auto',
    });

    engine.start({
      text: 'long run',
      attachments: [],
      sessionId: 'session-live',
    });

    await waitForCalls(calls);
    engine.disconnect();
    await new Promise((resolve) => setTimeout(resolve, 0));

    expect(engine.stage).toBe('idle');
    expect(useStreamingStore.getState().isStreaming).toBe(false);
    expect(useMessageStore.getState().messages.some((message) => message.role === 'system')).toBe(false);
  });
});

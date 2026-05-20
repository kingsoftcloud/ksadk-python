import { describe, expect, it } from 'vitest';
import { RunEngineImpl } from '../core/run/engine.js';
import type { ApiFacade } from '../core/api/types.js';

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

describe('RunEngineImpl', () => {
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
});

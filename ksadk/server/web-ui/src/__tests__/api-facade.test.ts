import { describe, it, expect } from 'vitest';
import { ApiFacadeImpl } from '../core/api/facade.js';
import type { ApiFacade } from '../core/api/types.js';

describe('ApiFacadeImpl', () => {
  it('exposes all required methods', () => {
    const facade = new ApiFacadeImpl();
    const methods: (keyof ApiFacade)[] = [
      'listSessions', 'createSession', 'deleteSession',
      'listSessionEvents', 'runAgent', 'subscribeRunEvents',
      'getResponseFeedback', 'upsertResponseFeedback', 'deleteResponseFeedback',
      'listWorkspaceFiles', 'addWorkspaceFile', 'deleteWorkspaceFile', 'getWorkspaceFileContent',
      'listAgentModels', 'getAgentUiBootstrap', 'uploadFile',
    ];
    for (const method of methods) {
      expect(typeof facade[method]).toBe('function');
    }
  });

  it('sends feedback payloads without rebuilding them as Message objects', async () => {
    const facade = new ApiFacadeImpl();
    const calls: Array<{ url: string; body: Record<string, unknown> }> = [];
    const originalFetch = globalThis.fetch;

    globalThis.fetch = (async (url, init) => {
      calls.push({
        url: String(url),
        body: JSON.parse(String(init?.body || '{}')) as Record<string, unknown>,
      });
      return new Response(JSON.stringify({
        Code: 0,
        Data: {
          Feedback: {
            ResponseId: 'resp-1',
            EventId: 'evt-1',
            Rating: 'up',
          },
        },
      }), { status: 200, headers: { 'content-type': 'application/json' } });
    }) as typeof fetch;

    try {
      await facade.upsertResponseFeedback({
        AgentId: 'agent-1',
        SessionId: 'session-1',
        ResponseId: 'resp-1',
        EventId: 'evt-1',
        Rating: 'up',
        Comment: '',
      });
    } finally {
      globalThis.fetch = originalFetch;
    }

    expect(calls).toEqual([
      {
        url: '/agentengine/api/v1/UpsertResponseFeedback',
        body: {
          AgentId: 'agent-1',
          SessionId: 'session-1',
          ResponseId: 'resp-1',
          EventId: 'evt-1',
          Rating: 'up',
          Comment: '',
        },
      },
    ]);
  });
});

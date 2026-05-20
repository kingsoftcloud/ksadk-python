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
      expect(typeof (facade as any)[method]).toBe('function');
    }
  });
});

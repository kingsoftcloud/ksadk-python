import type { ApiFacade } from './types.js';
import { postJsonAction, streamGetAction } from '../../api/client.js';
import { listSessions as listSessionsApi, createSession as createSessionApi, deleteSession as deleteSessionApi } from '../../api/session.js';
import { listSessionEvents as listSessionEventsApi } from '../../api/events.js';
import { runAgent as runAgentApi } from '../../api/run.js';
import { listWorkspaceFiles as listWorkspaceFilesApi, addWorkspaceFile as addWorkspaceFileApi, deleteWorkspaceFile as deleteWorkspaceFileApi, getWorkspaceFileContent as getFileContentApi } from '../../api/workspace.js';
import { listAgentModels as listAgentModelsApi } from '../../api/model.js';
import { getAgentUiBootstrap as getBootstrapApi } from '../../api/bootstrap.js';
import { uploadFile as uploadFileApi } from '../../api/upload.js';

export class ApiFacadeImpl implements ApiFacade {
  // Session
  async listSessions(agentId: string, opts?: { signal?: AbortSignal }) {
    const data = await listSessionsApi(agentId, opts);
    return data as unknown[];
  }

  async createSession(agentId: string, opts?: { signal?: AbortSignal }) {
    const data = await createSessionApi(agentId, opts);
    return { SessionId: data.SessionId };
  }

  async deleteSession(sessionId: string, opts?: { signal?: AbortSignal }) {
    await deleteSessionApi(sessionId, opts);
  }

  // Events & Run
  async listSessionEvents(sessionId: string) {
    return listSessionEventsApi(sessionId) as Promise<{ Events: unknown[] }>;
  }

  async runAgent(body: Record<string, unknown>, opts?: { signal?: AbortSignal }) {
    return runAgentApi(body, opts);
  }

  async subscribeRunEvents(
    params: { sessionId: string; invocationId: string; afterSeqId: number },
    opts?: { signal?: AbortSignal },
  ) {
    const qs: Record<string, string> = {
      SessionId: params.sessionId,
      InvocationId: params.invocationId,
      AfterSeqId: String(params.afterSeqId),
    };
    return streamGetAction('SubscribeRunEvents', qs, opts);
  }

  async cancelRun(agentId: string, invocationId: string, opts?: { signal?: AbortSignal }) {
    return postJsonAction('CancelRun', { AgentId: agentId, InvocationId: invocationId }, opts);
  }

  // Feedback
  async getResponseFeedback(payload: Record<string, unknown>, opts?: { signal?: AbortSignal }) {
    return postJsonAction('GetResponseFeedback', payload, opts);
  }

  async upsertResponseFeedback(payload: Record<string, unknown>, opts?: { signal?: AbortSignal }) {
    return postJsonAction('UpsertResponseFeedback', payload, opts);
  }

  async deleteResponseFeedback(payload: Record<string, unknown>, opts?: { signal?: AbortSignal }) {
    await postJsonAction('DeleteResponseFeedback', payload, opts);
  }

  // Workspace
  async listWorkspaceFiles(agentId: string, path: string, recursive: boolean) {
    return listWorkspaceFilesApi(agentId, path, recursive);
  }

  async addWorkspaceFile(formData: FormData) {
    return addWorkspaceFileApi(formData);
  }

  async deleteWorkspaceFile(agentId: string, path: string) {
    await deleteWorkspaceFileApi(agentId, path);
  }

  async getWorkspaceFileContent(agentId: string, path: string, opts?: { signal?: AbortSignal; asText?: boolean }) {
    return getFileContentApi(agentId, path, opts) as Promise<Blob | string>;
  }

  // Models & Bootstrap
  async listAgentModels(agentId: string) {
    return listAgentModelsApi(agentId);
  }

  async getAgentUiBootstrap() {
    return getBootstrapApi();
  }

  // Upload
  async uploadFile(formData: FormData, opts?: { signal?: AbortSignal }) {
    return uploadFileApi(formData, opts);
  }
}

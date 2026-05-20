import type { ApiFacade } from './types.js';
import { postJsonAction, postFormAction, getResource, streamAction, streamGetAction } from '../../api/client.js';
import { listSessions as listSessionsApi, createSession as createSessionApi, deleteSession as deleteSessionApi } from '../../api/session.js';
import { listSessionEvents as listSessionEventsApi } from '../../api/events.js';
import { runAgent as runAgentApi } from '../../api/run.js';
import { getResponseFeedback as getFeedbackApi, upsertResponseFeedback as upsertFeedbackApi, deleteResponseFeedback as deleteFeedbackApi } from '../../api/feedback.js';
import { listWorkspaceFiles as listWorkspaceFilesApi, addWorkspaceFile as addWorkspaceFileApi, deleteWorkspaceFile as deleteWorkspaceFileApi, getWorkspaceFileContent as getFileContentApi } from '../../api/workspace.js';
import { listAgentModels as listAgentModelsApi } from '../../api/model.js';
import { getAgentUiBootstrap as getBootstrapApi } from '../../api/bootstrap.js';
import { uploadFile as uploadFileApi } from '../../api/upload.js';
import type { Message } from '../../components/chat/types.js';
import { buildGetFeedbackPayload, buildUpsertFeedbackPayload } from '../../utils/feedback.js';

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
  async listSessionEvents(sessionId: string, opts?: { signal?: AbortSignal }) {
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

  // Feedback
  async getResponseFeedback(payload: Record<string, unknown>, opts?: { signal?: AbortSignal }) {
    const agentId = payload.AgentId as string;
    const sessionId = payload.SessionId as string;
    const message = payload as unknown as Message;
    return getFeedbackApi(agentId, sessionId, message);
  }

  async upsertResponseFeedback(payload: Record<string, unknown>, opts?: { signal?: AbortSignal }) {
    const agentId = payload.AgentId as string;
    const sessionId = payload.SessionId as string;
    const message = payload as unknown as Message;
    const rating = payload.Rating as string;
    const comment = payload.Comment as string | undefined;
    return upsertFeedbackApi(agentId, sessionId, message, rating, comment);
  }

  async deleteResponseFeedback(payload: Record<string, unknown>, opts?: { signal?: AbortSignal }) {
    const agentId = payload.AgentId as string;
    const sessionId = payload.SessionId as string;
    const message = payload as unknown as Message;
    await deleteFeedbackApi(agentId, sessionId, message);
  }

  // Workspace
  async listWorkspaceFiles(agentId: string, path: string, recursive: boolean, opts?: { signal?: AbortSignal }) {
    return listWorkspaceFilesApi(agentId, path, recursive);
  }

  async addWorkspaceFile(formData: FormData, opts?: { signal?: AbortSignal }) {
    return addWorkspaceFileApi(formData);
  }

  async deleteWorkspaceFile(agentId: string, path: string, opts?: { signal?: AbortSignal }) {
    await deleteWorkspaceFileApi(agentId, path);
  }

  async getWorkspaceFileContent(agentId: string, path: string, opts?: { signal?: AbortSignal; asText?: boolean }) {
    return getFileContentApi(agentId, path, opts) as Promise<Blob | string>;
  }

  // Models & Bootstrap
  async listAgentModels(agentId: string, opts?: { signal?: AbortSignal }) {
    return listAgentModelsApi(agentId);
  }

  async getAgentUiBootstrap(opts?: { signal?: AbortSignal }) {
    return getBootstrapApi();
  }

  // Upload
  async uploadFile(formData: FormData, opts?: { signal?: AbortSignal }) {
    return uploadFileApi(formData, opts);
  }
}

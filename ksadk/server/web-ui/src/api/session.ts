import { postJsonAction } from './client.js';

type SessionPayload = {
  SessionId: string;
  AgentId: string;
  UserId: string;
  Title?: string;
  Summary?: string;
  FirstPrompt?: string;
  LastPrompt?: string;
  UpdatedAt?: string;
  CreatedAt?: string;
  ActiveRunStatus?: string;
};

type ListSessionsResponse = {
  Sessions: SessionPayload[];
};

type CreateSessionResponse = {
  Session: SessionPayload;
};

export type { SessionPayload };

export async function listSessions(agentId: string, opts?: { signal?: AbortSignal }): Promise<SessionPayload[]> {
  const data = await postJsonAction<ListSessionsResponse>('ListSessions', {
    AgentId: agentId,
  }, opts);
  return data.Sessions ?? [];
}

export async function createSession(agentId: string, opts?: { signal?: AbortSignal }): Promise<SessionPayload> {
  const data = await postJsonAction<CreateSessionResponse>('CreateSession', {
    AgentId: agentId,
  }, opts);
  return data.Session;
}

export async function deleteSession(sessionId: string, opts?: { signal?: AbortSignal }): Promise<void> {
  await postJsonAction<unknown>('DeleteSession', { SessionId: sessionId }, opts);
}
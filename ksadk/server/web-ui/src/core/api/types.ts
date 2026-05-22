export interface ApiFacade {
  // Session
  listSessions(agentId: string, opts?: { signal?: AbortSignal }): Promise<unknown[]>;
  createSession(agentId: string, opts?: { signal?: AbortSignal }): Promise<{ SessionId: string }>;
  deleteSession(sessionId: string, opts?: { signal?: AbortSignal }): Promise<void>;

  // Events & Run
  listSessionEvents(sessionId: string, opts?: { signal?: AbortSignal }): Promise<{ Events: unknown[] }>;
  runAgent(body: Record<string, unknown>, opts?: { signal?: AbortSignal }): Promise<ReadableStream<Uint8Array>>;
  subscribeRunEvents(params: { sessionId: string; invocationId: string; afterSeqId: number }, opts?: { signal?: AbortSignal }): Promise<ReadableStream<Uint8Array>>;
  cancelRun(agentId: string, invocationId: string, opts?: { signal?: AbortSignal }): Promise<unknown>;

  // Feedback
  getResponseFeedback(payload: Record<string, unknown>, opts?: { signal?: AbortSignal }): Promise<unknown>;
  upsertResponseFeedback(payload: Record<string, unknown>, opts?: { signal?: AbortSignal }): Promise<unknown>;
  deleteResponseFeedback(payload: Record<string, unknown>, opts?: { signal?: AbortSignal }): Promise<void>;

  // Workspace
  listWorkspaceFiles(agentId: string, path: string, recursive: boolean, opts?: { signal?: AbortSignal }): Promise<unknown>;
  addWorkspaceFile(formData: FormData, opts?: { signal?: AbortSignal }): Promise<unknown>;
  deleteWorkspaceFile(agentId: string, path: string, opts?: { signal?: AbortSignal }): Promise<void>;
  getWorkspaceFileContent(agentId: string, path: string, opts?: { signal?: AbortSignal; asText?: boolean }): Promise<Blob | string>;

  // Models & Bootstrap
  listAgentModels(agentId: string, opts?: { signal?: AbortSignal }): Promise<unknown>;
  getAgentUiBootstrap(opts?: { signal?: AbortSignal }): Promise<unknown>;

  // Upload
  uploadFile(formData: FormData, opts?: { signal?: AbortSignal }): Promise<{ FileData: { fileUri: string; displayName: string; mimeType: string } }>;
}

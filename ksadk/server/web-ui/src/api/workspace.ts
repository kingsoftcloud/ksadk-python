import { postJsonAction, postFormAction, getResource } from './client.js';

export async function listWorkspaceFiles(
  agentId: string,
  path: string,
  recursive: boolean,
): Promise<unknown> {
  return postJsonAction('ListWorkspaceFiles', {
    AgentId: agentId,
    Path: path,
    Recursive: recursive,
  });
}

export async function addWorkspaceFile(formData: FormData): Promise<unknown> {
  return postFormAction('AddWorkspaceFile', formData);
}

export async function deleteWorkspaceFile(agentId: string, path: string): Promise<unknown> {
  return postJsonAction('DeleteWorkspaceFile', { AgentId: agentId, Path: path });
}

export async function getWorkspaceFileContent(
  agentId: string,
  path: string,
  opts?: { signal?: AbortSignal; asText?: boolean },
): Promise<unknown> {
  return getResource('GetWorkspaceFileContent', { AgentId: agentId, FilePath: path }, { signal: opts?.signal, asText: opts?.asText ?? true });
}

import { postJsonAction } from './client.js';

export async function listAgentModels(agentId: string): Promise<unknown> {
  return postJsonAction('ListAgentModels', { AgentId: agentId });
}
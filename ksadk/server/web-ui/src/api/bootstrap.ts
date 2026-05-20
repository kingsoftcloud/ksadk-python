import { postJsonAction } from './client.js';

export async function getAgentUiBootstrap(): Promise<unknown> {
  return postJsonAction('GetAgentUiBootstrap', {});
}
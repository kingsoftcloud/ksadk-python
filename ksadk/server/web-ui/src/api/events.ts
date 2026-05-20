import { postJsonAction } from './client.js';

export async function listSessionEvents(sessionId: string): Promise<unknown> {
  return postJsonAction('ListSessionEvents', { SessionId: sessionId });
}
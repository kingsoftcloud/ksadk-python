import { postJsonAction } from './client.js';
import { buildGetFeedbackPayload, buildUpsertFeedbackPayload } from '../utils/feedback.js';
import type { Message } from '../components/chat/types.js';

export async function getResponseFeedback(
  agentId: string,
  sessionId: string,
  message: Message,
): Promise<unknown> {
  return postJsonAction('GetResponseFeedback', buildGetFeedbackPayload({ agentId, sessionId, message }));
}

export async function upsertResponseFeedback(
  agentId: string,
  sessionId: string,
  message: Message,
  rating: string,
  comment?: string,
): Promise<unknown> {
  return postJsonAction('UpsertResponseFeedback', buildUpsertFeedbackPayload({ agentId, sessionId, message, rating, comment }));
}

export async function deleteResponseFeedback(
  agentId: string,
  sessionId: string,
  message: Message,
): Promise<unknown> {
  return postJsonAction('DeleteResponseFeedback', buildGetFeedbackPayload({ agentId, sessionId, message }));
}
/**
 * @typedef {import('../components/chat/types.js').Message} ChatMessage
 * @typedef {NonNullable<ChatMessage['feedback']>} ChatMessageFeedback
 */

function textValue(value) {
  return String(value || '').trim();
}

function normalizedRating(value) {
  const rating = textValue(value).toLowerCase();
  return rating === 'up' || rating === 'down' ? rating : '';
}

/**
 * @param {unknown} value
 * @returns {ChatMessageFeedback | null}
 */
export function normalizeFeedback(value) {
  if (!value || typeof value !== 'object') {
    return null;
  }
  const rating = normalizedRating(value.Rating || value.rating);
  if (!rating) {
    return null;
  }
  return {
    agentId: textValue(value.AgentId || value.agentId),
    sessionId: textValue(value.SessionId || value.sessionId),
    responseId: textValue(value.ResponseId || value.responseId),
    eventId: textValue(value.EventId || value.eventId),
    rating,
    comment: String(value.Comment ?? value.comment ?? ''),
    traceId: textValue(value.TraceId || value.traceId),
    rootSpanId: textValue(value.RootSpanId || value.rootSpanId),
    updatedAt: textValue(value.UpdatedAt || value.updatedAt),
  };
}

/**
 * @param {ChatMessage | null | undefined} message
 * @param {boolean} isStreaming
 * @param {boolean} isLastMessage
 */
export function shouldRenderFeedbackControls(message, isStreaming, isLastMessage) {
  return Boolean(
    message?.role === 'model' &&
      textValue(message.responseId) &&
      textValue(message.eventId) &&
      !(isStreaming && isLastMessage),
  );
}

/**
 * @param {{ agentId: string; sessionId: string; message: ChatMessage | null | undefined }} options
 */
export function buildGetFeedbackPayload({ agentId, sessionId, message }) {
  const responseId = textValue(message?.responseId);
  if (!responseId) {
    throw new Error('Message missing response id');
  }
  return {
    AgentId: textValue(agentId),
    SessionId: textValue(sessionId),
    ResponseId: responseId,
  };
}

/**
 * @param {{
 *   agentId: string;
 *   sessionId: string;
 *   message: ChatMessage | null | undefined;
 *   rating: string;
 *   comment?: string;
 * }} options
 */
export function buildUpsertFeedbackPayload({
  agentId,
  sessionId,
  message,
  rating,
  comment = '',
}) {
  const normalized = normalizedRating(rating);
  if (!normalized) {
    throw new Error('Feedback rating must be up or down');
  }
  const payload = {
    ...buildGetFeedbackPayload({ agentId, sessionId, message }),
    EventId: textValue(message?.eventId),
    Rating: normalized,
    Comment: String(comment || ''),
    TraceId: textValue(message?.traceId),
    RootSpanId: textValue(message?.rootSpanId),
  };
  if (!payload.EventId) {
    throw new Error('Message missing event id');
  }
  return payload;
}

/**
 * @param {ChatMessage[]} messages
 * @param {{ messageId: string; rating: string; comment?: string }} options
 * @returns {{ nextMessages: ChatMessage[]; previousFeedback: ChatMessageFeedback | null }}
 */
export function applyOptimisticFeedback(messages, { messageId, rating, comment = '' }) {
  const previousMessage = messages.find((message) => message.id === messageId);
  const previousFeedback = previousMessage?.feedback ? { ...previousMessage.feedback } : null;
  const normalized = normalizedRating(rating);
  const nextMessages = messages.map((message) =>
    message.id === messageId
      ? {
          ...message,
          feedback: {
            ...(message.feedback || {}),
            responseId: textValue(message.responseId),
            eventId: textValue(message.eventId),
            rating: normalized,
            comment: String(comment || ''),
            pending: true,
            error: '',
          },
        }
      : message,
  );
  return { nextMessages, previousFeedback };
}

/**
 * @param {ChatMessage[]} messages
 * @param {{ messageId: string; feedback: unknown }} options
 * @returns {ChatMessage[]}
 */
export function markFeedbackSaved(messages, { messageId, feedback }) {
  const normalized = normalizeFeedback(feedback) || feedback;
  return messages.map((message) =>
    message.id === messageId
      ? {
          ...message,
          feedback: normalized
            ? {
                ...normalized,
                pending: false,
                error: '',
              }
            : undefined,
        }
      : message,
  );
}

/**
 * @param {ChatMessage[]} messages
 * @param {{ messageId: string; previousFeedback: ChatMessageFeedback | null }} options
 * @returns {ChatMessage[]}
 */
export function rollbackFeedback(messages, { messageId, previousFeedback }) {
  return messages.map((message) => {
    if (message.id !== messageId) {
      return message;
    }
    if (!previousFeedback) {
      const { feedback, ...rest } = message;
      return rest;
    }
    return {
      ...message,
      feedback: previousFeedback,
    };
  });
}

/**
 * @param {ChatMessage[]} messages
 * @param {{ messageId: string }} options
 * @returns {ChatMessage[]}
 */
export function clearFeedback(messages, { messageId }) {
  return messages.map((message) => {
    if (message.id !== messageId) {
      return message;
    }
    const { feedback, ...rest } = message;
    return rest;
  });
}

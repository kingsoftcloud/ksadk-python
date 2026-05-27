import {
  createResponsesStreamState,
  normalizeResponsesStreamEvent,
} from './responses-stream.js';

/**
 * @typedef {import('../components/chat/types.js').Message} Message
 */

const RUN_TERMINAL_STATUSES = new Set([
  'completed',
  'failed',
  'error',
  'cancelled',
  'canceled',
  'aborted',
]);

function textFromUnknown(value) {
  if (typeof value === 'string') {
    return value;
  }
  if (Array.isArray(value)) {
    return value
      .map((item) => {
        if (typeof item === 'string') return item;
        if (item && typeof item === 'object' && typeof item.text === 'string') {
          return item.text;
        }
        return '';
      })
      .join('');
  }
  return '';
}

function attachmentContentUrl(fileUri) {
  const normalized = String(fileUri || '').trim();
  if (!normalized) {
    return '';
  }
  return `/agentengine/api/v1/AttachmentContent?FileUri=${encodeURIComponent(normalized)}`;
}

function mimeTypeFromDataUrl(dataUrl) {
  const match = String(dataUrl || '').match(/^data:([^;,]+)/);
  return match?.[1] || '';
}

function mimeTypesMatch(left, right) {
  const normalizedLeft = String(left || '').trim();
  const normalizedRight = String(right || '').trim();
  if (!normalizedLeft || !normalizedRight) {
    return true;
  }
  if (normalizedLeft === normalizedRight) {
    return true;
  }
  if (normalizedLeft.endsWith('/*')) {
    return normalizedRight.startsWith(normalizedLeft.slice(0, -1));
  }
  if (normalizedRight.endsWith('/*')) {
    return normalizedLeft.startsWith(normalizedRight.slice(0, -1));
  }
  return false;
}

export function parseMessageContent(event) {
  const parts = event?.Content?.parts || [];
  const textSegments = [];
  const attachmentsByKey = new Map();

  const pushAttachment = (attachment) => {
    const key = `${attachment.fileUri || attachment.url || attachment.name}|${attachment.type}`;
    if (!attachmentsByKey.has(key)) {
      attachmentsByKey.set(key, attachment);
    }
  };

  for (const part of parts) {
    if (part?.type === 'input_text' || part?.text) {
      textSegments.push(part.text || '');
      continue;
    }
    if (part?.type === 'input_file' && part.inlineData) {
      pushAttachment({
        name: part.inlineData.displayName || 'attachment',
        url: `data:${part.inlineData.mimeType || 'application/octet-stream'};base64,${part.inlineData.data}`,
        type: part.inlineData.mimeType || 'application/octet-stream',
      });
      continue;
    }
    if (part?.type === 'input_file' && typeof part.file_data === 'string' && part.file_data) {
      pushAttachment({
        name: part.filename || part.displayName || part.display_name || 'attachment',
        url: `data:${part.mime_type || part.mimeType || 'application/octet-stream'};base64,${part.file_data}`,
        type: part.mime_type || part.mimeType || 'application/octet-stream',
      });
      continue;
    }
    if (part?.type === 'input_file' && typeof part.file_url === 'string' && part.file_url.trim()) {
      const fileUri = part.file_url.trim();
      pushAttachment({
        name: part.filename || part.displayName || part.display_name || 'attachment',
        url: attachmentContentUrl(fileUri),
        type: part.mime_type || part.mimeType || 'application/octet-stream',
        fileUri,
      });
      continue;
    }
    if (part?.type === 'input_image' || part?.type === 'image_url') {
      const imageUrl = typeof part.image_url === 'string'
        ? part.image_url
        : part.image_url?.url;
      if (imageUrl) {
        pushAttachment({
          name: part.filename || part.displayName || part.display_name || 'uploaded_image',
          url: imageUrl,
          type: part.mime_type || part.mimeType || mimeTypeFromDataUrl(imageUrl) || 'image/*',
        });
      }
      continue;
    }
    if (part?.type === 'input_file' && part.fileData) {
      const fileUri = String(part.fileData.fileUri || '').trim();
      pushAttachment({
        name: part.fileData.displayName || 'attachment',
        url: attachmentContentUrl(fileUri),
        type: part.fileData.mimeType || 'application/octet-stream',
        fileUri,
      });
    }
  }

  const metadataAttachments = Array.isArray(event?.Metadata?.attachments)
    ? event.Metadata.attachments
    : [];

  for (const attachment of metadataAttachments) {
    const fileUri = String(attachment.file_uri || '').trim();
    const metadataAttachment = {
      name: attachment.display_name || 'attachment',
      url: attachmentContentUrl(fileUri),
      type: attachment.mime_type || 'application/octet-stream',
      fileUri,
    };
    const isEmptyMetadataReference = !metadataAttachment.url && !metadataAttachment.fileUri;
    const isAlreadyRepresentedByContent = isEmptyMetadataReference
      && Array.from(attachmentsByKey.values()).some((existing) => (
        existing.name === metadataAttachment.name
        && mimeTypesMatch(existing.type, metadataAttachment.type)
        && (existing.url || existing.fileUri)
      ));
    if (!isAlreadyRepresentedByContent) {
      pushAttachment(metadataAttachment);
    }
  }

  const attachments = Array.from(attachmentsByKey.values());
  return {
    text: textSegments.join(''),
    attachments: attachments.length > 0 ? attachments : undefined,
  };
}

function buildResponsesOutputEnhancements(event) {
  const responsesOutput = event?.Metadata?.responses_output;
  if (!Array.isArray(responsesOutput)) {
    return {};
  }

  const state = createResponsesStreamState();
  const actions = normalizeResponsesStreamEvent({
    eventName: 'response.completed',
    data: {
      response: {
        id: String(event.Metadata?.response_id || ''),
        output: responsesOutput,
      },
    },
    state,
  });
  let reasoning = '';
  const tools = {};

  for (const action of actions) {
    if (action.type === 'reasoning_delta') {
      reasoning += action.text;
      continue;
    }
    if (action.type === 'tool_upsert') {
      tools[action.name] = {
        ...(tools[action.name] || { name: action.name, args: '' }),
        name: action.name,
        args: action.args,
        status: action.status,
        ...(action.approvalRequestId ? { approvalRequestId: action.approvalRequestId } : {}),
        ...(action.previousResponseId ? { previousResponseId: action.previousResponseId } : {}),
        ...(action.serverLabel ? { serverLabel: action.serverLabel } : {}),
        ...(action.approvalRequestId ? { approvalStatus: 'pending' } : {}),
      };
      continue;
    }
    if (action.type === 'tool_result') {
      tools[action.name] = {
        ...(tools[action.name] || { name: action.name, args: '' }),
        name: action.name,
        output: action.output,
        status: 'completed',
      };
    }
  }

  return {
    ...(reasoning ? { reasoning } : {}),
    ...(Object.keys(tools).length > 0 ? { tools } : {}),
  };
}

function buildCompactionLabel(trigger, status, historical) {
  if (status === 'running') {
    return trigger === 'prompt_too_long'
      ? '检测到上下文过长，正在自动压缩历史后重试'
      : '正在自动压缩上下文';
  }
  if (historical) {
    return trigger === 'prompt_too_long'
      ? '上下文过长，系统已自动压缩历史并重试'
      : '系统已自动压缩较早的对话上下文';
  }
  if (status === 'failed') {
    return '自动压缩上下文未完成';
  }
  return trigger === 'prompt_too_long'
    ? '已完成上下文压缩，并继续当前回复'
    : '已完成上下文压缩';
}

function assistantOutputDedupeKey(invocationId, message) {
  return [
    invocationId,
    message.content || '',
    message.reasoning || '',
  ].join('\u0000');
}

function normalizedMessageContent(message) {
  return [
    String(message?.role || ''),
    String(message?.content || '').replace(/\s+/g, ' ').trim(),
    String(message?.reasoning || '').replace(/\s+/g, ' ').trim(),
  ].join('\u0000');
}

function messageContentDedupeKey(message) {
  const normalized = normalizedMessageContent(message);
  const attachmentKey = (message?.attachments || [])
    .map((attachment) => `${attachment.name || ''}|${attachment.type || ''}|${attachment.fileUri || attachment.url || ''}`)
    .join('\u0001');
  return `${normalized}\u0000${attachmentKey}`;
}

function stringifyToolPayload(value) {
  if (typeof value === 'string') {
    return value;
  }
  if (value === undefined || value === null) {
    return '';
  }
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

function toolMessageFromSessionEvent(event) {
  const eventType = String(event?.EventType || '');
  if (eventType !== 'tool_call' && eventType !== 'tool_result') {
    return null;
  }
  const name = String(
    event.Metadata?.tool_name
      || event.Metadata?.name
      || event.Metadata?.function_name
      || textFromUnknown(event.Content?.parts)
      || 'tool',
  ).trim() || 'tool';
  const existing = {
    name,
    args: '',
    status: eventType === 'tool_result' ? 'completed' : 'running',
  };
  const tool =
    eventType === 'tool_result'
      ? {
          ...existing,
          output: stringifyToolPayload(
            event.Metadata?.tool_output
              ?? event.Metadata?.output
              ?? event.Metadata?.result
              ?? textFromUnknown(event.Content?.parts),
          ),
          status: 'completed',
        }
      : {
          ...existing,
          args: stringifyToolPayload(
            event.Metadata?.tool_args
              ?? event.Metadata?.arguments
              ?? event.Metadata?.args
              ?? {},
          ),
        };
  return {
    id: event.EventId || String(Date.now() + Math.random()),
    role: 'model',
    content: '',
    timestamp: event.Timestamp || Date.now(),
    eventType,
    tools: {
      [name]: tool,
    },
  };
}

function mergeToolMaps(first = {}, second = {}) {
  const merged = { ...(first || {}) };
  for (const [name, tool] of Object.entries(second || {})) {
    merged[name] = {
      ...(merged[name] || {}),
      ...(tool || {}),
      name,
      args: tool?.args || merged[name]?.args || '',
    };
  }
  return merged;
}

function messageVisibleContentDedupeKey(message) {
  const attachmentKey = (message?.attachments || [])
    .map((attachment) => `${attachment.name || ''}|${attachment.type || ''}|${attachment.fileUri || attachment.url || ''}`)
    .join('\u0001');
  return [
    String(message?.role || ''),
    String(message?.content || '').replace(/\s+/g, ' ').trim(),
    attachmentKey,
  ].join('\u0000');
}

function mergeMessageMetadata(existing, incoming) {
  return {
    ...existing,
    reasoning: mergeReasoningText(existing.reasoning, incoming.reasoning),
    tools: {
      ...(existing.tools || {}),
      ...(incoming.tools || {}),
    },
    eventId: incoming.eventId || existing.eventId,
    responseId: incoming.responseId || existing.responseId,
    traceId: incoming.traceId || existing.traceId,
    rootSpanId: incoming.rootSpanId || existing.rootSpanId,
    timestamp: Math.max(Number(existing.timestamp || 0), Number(incoming.timestamp || 0)) || incoming.timestamp || existing.timestamp,
    id: incoming.responseId || incoming.reasoning || incoming.tools ? incoming.id : existing.id,
  };
}

function mergeReasoningText(first = '', second = '') {
  const left = String(first || '');
  const right = String(second || '');
  if (!left) return right;
  if (!right) return left;
  if (left.endsWith(right)) return left;
  if (right.startsWith(left)) return right;
  return `${left}${right}`;
}

/**
 * @param {{
 *   id: string;
 *   timestamp: number;
 *   status?: Message['status'];
 *   trigger?: string;
 *   compactedUntilSeqId?: number;
 *   summary?: string;
 *   historical?: boolean;
 * }} options
 * @returns {Message}
 */
export function buildCompactionMessage(options) {
  return {
    id: options.id,
    role: 'system',
    eventType: 'context_checkpoint',
    status: options.status,
    trigger: options.trigger,
    compactedUntilSeqId: options.compactedUntilSeqId,
    summary: options.summary,
    historical: options.historical,
    timestamp: options.timestamp,
    content: buildCompactionLabel(options.trigger, options.status, options.historical),
  };
}

/**
 * @param {import('../types/session-events.js').SessionEventRecord} event
 * @returns {Message | null}
 */
export function buildMessageFromSessionEvent(event) {
  const eventType = event?.EventType || '';
  if (eventType === 'run_status') {
    if (event.Content?.status === 'failed') {
      return {
        id: event.EventId || String(Date.now() + Math.random()),
        role: 'system',
        content: event.Content?.detail || '本轮运行失败。',
        eventType,
        status: 'failed',
        timestamp: event.Timestamp || Date.now(),
      };
    }
    if (event.Content?.status === 'cancelled') {
      return {
        id: event.EventId || String(Date.now() + Math.random()),
        role: 'system',
        content: event.Content?.detail || '本轮输出已停止。',
        eventType,
        status: 'cancelled',
        timestamp: event.Timestamp || Date.now(),
      };
    }
    return null;
  }

  if (eventType === 'context_checkpoint') {
    const parsed = parseMessageContent(event);
    return buildCompactionMessage({
      id: event.EventId || String(Date.now() + Math.random()),
      timestamp: event.Timestamp || Date.now(),
      status: 'completed',
      trigger: String(event.Metadata?.trigger || 'auto'),
      compactedUntilSeqId: Number(event.Metadata?.compacted_until_seq_id || 0) || undefined,
      summary: parsed.text || undefined,
      historical: true,
    });
  }

  if (eventType === 'reasoning') {
    const parsed = parseMessageContent(event);
    const reasoning = parsed.text || textFromUnknown(event.Metadata?.reasoning);
    return reasoning
      ? {
          id: event.EventId || String(Date.now() + Math.random()),
          role: 'model',
          content: '',
          reasoning,
          timestamp: event.Timestamp || Date.now(),
          eventType,
        }
      : null;
  }

  if (eventType === 'tool_call' || eventType === 'tool_result') {
    return toolMessageFromSessionEvent(event);
  }

  if (eventType !== 'user_message' && eventType !== 'assistant_message') {
    return null;
  }

  const parsed = parseMessageContent(event);
  const responsesEnhancements =
    eventType === 'assistant_message' ? buildResponsesOutputEnhancements(event) : {};
  if (
    !parsed.text
    && !parsed.attachments?.length
    && !responsesEnhancements.reasoning
    && !responsesEnhancements.tools
  ) {
    return null;
  }

  const responseId = String(event.Metadata?.response_id || event.Metadata?.ResponseId || '').trim();
  const traceId = String(event.Metadata?.trace_id || event.Metadata?.TraceId || '').trim();
  const rootSpanId = String(
    event.Metadata?.root_span_id || event.Metadata?.rootSpanId || event.Metadata?.RootSpanId || '',
  ).trim();

  return {
    id: event.EventId || String(Date.now() + Math.random()),
    role: eventType === 'user_message' ? 'user' : 'model',
    content: parsed.text,
    timestamp: event.Timestamp || Date.now(),
    eventType,
    eventId: event.EventId || undefined,
    responseId: responseId || undefined,
    traceId: traceId || undefined,
    rootSpanId: rootSpanId || undefined,
    attachments: parsed.attachments,
    ...responsesEnhancements,
  };
}

/**
 * @param {import('../types/session-events.js').SessionEventRecord[]} [events]
 * @returns {Message[]}
 */
export function buildMessagesFromSessionEvents(events = []) {
  const latestRunStatusByInvocation = new Map();
  const outputByInvocation = new Set();
  const assistantOutputKeys = new Set();
  const seenEventIds = new Set();
  const normalizedEvents = Array.isArray(events) ? events : [];
  const uniqueEvents = [];
  for (const event of normalizedEvents) {
    const eventId = String(event?.EventId || '').trim();
    if (eventId) {
      if (seenEventIds.has(eventId)) {
        continue;
      }
      seenEventIds.add(eventId);
    }
    uniqueEvents.push(event);
  }

  for (const event of uniqueEvents) {
    const invocationId = String(event.InvocationId || '').trim();
    if (
      invocationId &&
      ['assistant_message', 'reasoning', 'tool_call'].includes(String(event.EventType || ''))
    ) {
      outputByInvocation.add(invocationId);
    }
    if (event.EventType !== 'run_status') {
      continue;
    }
    if (!invocationId) {
      continue;
    }
    latestRunStatusByInvocation.set(invocationId, String(event.Content?.status || '').trim());
  }

  /** @type {Array<Message & { invocationId?: string }>} */
  const messages = [];
  const messageContentKeys = new Set();
  const messageVisibleKeyToIndex = new Map();
  /** @type {(Message & { invocationId?: string }) | null} */
  let pendingReasoning = null;
  /** @type {Map<string, Message & { invocationId?: string }>} */
  const pendingToolsByInvocation = new Map();

  const pushMessage = (message) => {
    const key = messageContentDedupeKey(message);
    if (key && messageContentKeys.has(key)) {
      return;
    }
    const visibleKey = messageVisibleContentDedupeKey(message);
    const existingIndex = messageVisibleKeyToIndex.get(visibleKey);
    if (existingIndex !== undefined) {
      messages[existingIndex] = mergeMessageMetadata(messages[existingIndex], message);
      if (key) {
        messageContentKeys.add(key);
      }
      return;
    }
    if (key) {
      messageContentKeys.add(key);
    }
    if (visibleKey) {
      messageVisibleKeyToIndex.set(visibleKey, messages.length);
    }
    messages.push(message);
  };

  const flushPendingReasoning = () => {
    if (pendingReasoning) {
      pushMessage(pendingReasoning);
      pendingReasoning = null;
    }
  };

  const takePendingTools = (invocationId) => {
    const normalizedInvocationId = String(invocationId || '').trim();
    if (!normalizedInvocationId) {
      return {};
    }
    const pendingTools = pendingToolsByInvocation.get(normalizedInvocationId);
    if (!pendingTools) {
      return {};
    }
    pendingToolsByInvocation.delete(normalizedInvocationId);
    return {
      tools: pendingTools.tools,
    };
  };

  for (const event of uniqueEvents) {
    if (event.EventType === 'run_status') {
      continue;
    }
    const message = buildMessageFromSessionEvent(event);
    if (!message) {
      continue;
    }
    if (message.eventType === 'tool_call' || message.eventType === 'tool_result') {
      const invocationId = String(event.InvocationId || '').trim();
      if (!invocationId) {
        flushPendingReasoning();
        pushMessage(message);
        continue;
      }
      const existing = pendingToolsByInvocation.get(invocationId);
      pendingToolsByInvocation.set(invocationId, {
        ...(existing || {
          id: message.id,
          role: 'model',
          content: '',
          timestamp: message.timestamp,
          invocationId,
        }),
        timestamp: Math.max(
          Number(existing?.timestamp || 0),
          Number(message.timestamp || 0),
        ),
        tools: mergeToolMaps(existing?.tools, message.tools),
      });
      continue;
    }
    if (message.eventType === 'reasoning') {
      const invocationId = String(event.InvocationId || '').trim();
      if (
        pendingReasoning &&
        invocationId &&
        pendingReasoning.invocationId === invocationId
      ) {
        pendingReasoning.reasoning = `${pendingReasoning.reasoning || ''}${message.reasoning || ''}`;
        pendingReasoning.timestamp = message.timestamp;
        continue;
      }
      flushPendingReasoning();
      pendingReasoning = {
        ...message,
        ...(invocationId ? { invocationId } : {}),
      };
      continue;
    }
    if (message.eventType === 'assistant_message' && pendingReasoning) {
      const invocationId = String(event.InvocationId || '').trim();
      const outputKey = assistantOutputDedupeKey(invocationId, message);
      if (invocationId && assistantOutputKeys.has(outputKey)) {
        pendingReasoning = null;
        continue;
      }
      if (invocationId) {
        assistantOutputKeys.add(outputKey);
      }
      pushMessage({
        ...message,
        ...takePendingTools(invocationId),
        reasoning: mergeReasoningText(pendingReasoning.reasoning, message.reasoning),
      });
      pendingReasoning = null;
      continue;
    }
    flushPendingReasoning();
    if (message.eventType === 'assistant_message') {
      const invocationId = String(event.InvocationId || '').trim();
      const outputKey = assistantOutputDedupeKey(invocationId, message);
      if (invocationId && assistantOutputKeys.has(outputKey)) {
        continue;
      }
      if (invocationId) {
        assistantOutputKeys.add(outputKey);
      }
      pushMessage({
        ...message,
        ...takePendingTools(invocationId),
      });
      continue;
    }
    pushMessage(message);
  }

  flushPendingReasoning();
  for (const pendingTools of pendingToolsByInvocation.values()) {
    pushMessage(pendingTools);
  }

  return messages.map(({ invocationId: _invocationId, ...message }) => message);
}

export function mergeSessionEventRecords(baseEvents = [], incomingEvents = []) {
  const merged = [];
  const seenEventIds = new Set();
  for (const event of [...(Array.isArray(baseEvents) ? baseEvents : []), ...(Array.isArray(incomingEvents) ? incomingEvents : [])]) {
    const eventId = String(event?.EventId || '').trim();
    if (eventId) {
      if (seenEventIds.has(eventId)) {
        continue;
      }
      seenEventIds.add(eventId);
    }
    merged.push(event);
  }
  return merged.sort((left, right) => {
    const leftSeqId = Number(left?.SeqId || 0);
    const rightSeqId = Number(right?.SeqId || 0);
    if (Number.isFinite(leftSeqId) && Number.isFinite(rightSeqId) && leftSeqId !== rightSeqId) {
      return leftSeqId - rightSeqId;
    }
    return Number(left?.Timestamp || 0) - Number(right?.Timestamp || 0);
  });
}

export function maxSeqIdFromEvents(events = []) {
  return (Array.isArray(events) ? events : []).reduce((maxSeqId, event) => {
    const seqId = Number(event.SeqId || 0);
    return Number.isFinite(seqId) ? Math.max(maxSeqId, seqId) : maxSeqId;
  }, 0);
}

export function eventHasTerminalRunStatus(event) {
  if (event?.EventType !== 'run_status') {
    return false;
  }
  return RUN_TERMINAL_STATUSES.has(String(event.Content?.status || '').trim().toLowerCase());
}

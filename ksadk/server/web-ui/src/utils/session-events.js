import {
  createResponsesStreamState,
  normalizeResponsesStreamEvent,
} from './responses-stream.js';

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
    pushAttachment({
      name: attachment.display_name || 'attachment',
      url: attachmentContentUrl(fileUri),
      type: attachment.mime_type || 'application/octet-stream',
      fileUri,
    });
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

  return {
    id: event.EventId || String(Date.now() + Math.random()),
    role: eventType === 'user_message' ? 'user' : 'model',
    content: parsed.text,
    timestamp: event.Timestamp || Date.now(),
    eventType,
    attachments: parsed.attachments,
    ...responsesEnhancements,
  };
}

export function buildMessagesFromSessionEvents(events = []) {
  const latestRunStatusByInvocation = new Map();
  const outputByInvocation = new Set();
  const normalizedEvents = Array.isArray(events) ? events : [];
  for (const event of normalizedEvents) {
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

  const messages = [];
  let pendingReasoning = null;

  const flushPendingReasoning = () => {
    if (pendingReasoning) {
      messages.push(pendingReasoning);
      pendingReasoning = null;
    }
  };

  for (const event of normalizedEvents) {
    if (event.EventType === 'run_status' && event.Content?.status === 'in_progress') {
        const invocationId = String(event.InvocationId || '').trim();
        if (
          invocationId &&
          latestRunStatusByInvocation.get(invocationId) === 'in_progress' &&
          !outputByInvocation.has(invocationId)
        ) {
          flushPendingReasoning();
          messages.push({
            id: event.EventId || String(Date.now() + Math.random()),
            role: 'system',
            content: '上一轮消息仍在运行中，正在等待运行时继续返回结果。',
            eventType: 'run_status',
            status: 'running',
            timestamp: event.Timestamp || Date.now(),
          });
        }
        continue;
      }
    const message = buildMessageFromSessionEvent(event);
    if (!message) {
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
    flushPendingReasoning();
    messages.push(message);
  }

  flushPendingReasoning();
  return messages.map(({ invocationId: _invocationId, ...message }) => message);
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

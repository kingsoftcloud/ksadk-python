export function createResponsesStreamState() {
  return {
    toolNames: new Map(),
    toolArguments: new Map(),
    callIds: new Map(),
    currentResponseId: '',
  };
}

export function textFromUnknown(value) {
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
  if (value && typeof value === 'object' && typeof value.text === 'string') {
    return value.text;
  }
  return '';
}

function stringifyPayload(value) {
  if (value === undefined || value === null) {
    return '';
  }
  if (typeof value === 'string') {
    return value;
  }
  if (typeof value === 'object') {
    return JSON.stringify(value, null, 2);
  }
  return String(value);
}

function firstString(...values) {
  for (const value of values) {
    const text = textFromUnknown(value);
    if (text) return text;
  }
  return '';
}

function itemKey(item, data) {
  return String(
    item?.id ||
      item?.item_id ||
      item?.call_id ||
      data?.item_id ||
      data?.call_id ||
      data?.output_index ||
      '',
  );
}

function rememberTool(state, key, item, name, args) {
  if (key) {
    state.toolNames.set(key, name);
    state.toolArguments.set(key, args);
  }
  const callId = String(item?.call_id || '');
  if (callId) {
    state.callIds.set(callId, key || name);
    state.toolNames.set(callId, name);
    state.toolArguments.set(callId, args);
  }
}

function toolNameFor(state, key, item, fallback = 'tool') {
  const callId = String(item?.call_id || '');
  return String(
    item?.name ||
      item?.tool_name ||
      (key ? state.toolNames.get(key) : '') ||
      (callId ? state.toolNames.get(callId) : '') ||
      fallback,
  );
}

function extractItemText(item) {
  if (!item || typeof item !== 'object') {
    return '';
  }
  const contentText = textFromUnknown(item.content);
  return firstString(item.output_text, item.text, item.summary_text, item.summary, item.delta, contentText);
}

function extractCompletedText(data) {
  const payload = data?.response && typeof data.response === 'object' ? data.response : data;
  const direct = firstString(payload?.output_text, payload?.text);
  if (direct) {
    return direct;
  }

  const output = Array.isArray(payload?.output) ? payload.output : [];
  for (const item of output) {
    const text = extractItemText(item);
    if (text) {
      return text;
    }
  }
  return '';
}

function completedOutputItems(data) {
  const payload = data?.response && typeof data.response === 'object' ? data.response : data;
  return Array.isArray(payload?.output) ? payload.output : [];
}

function normalizeOutputItem({ data, state, status }) {
  const item = data?.item || data?.output_item || data || {};
  const type = String(item.type || '').trim();
  const key = itemKey(item, data);

  if (type === 'function_call') {
    const name = toolNameFor(state, key, item);
    const args = stringifyPayload(item.arguments ?? item.args ?? item.input);
    rememberTool(state, key, item, name, args);
    return [{ type: 'tool_upsert', name, args, status }];
  }

  if (type === 'function_call_output') {
    const name = toolNameFor(state, key, item);
    return [
      {
        type: 'tool_result',
        name,
        output: stringifyPayload(item.output ?? item.result ?? item.content),
      },
    ];
  }

  if (type === 'mcp_approval_request') {
    const name = String(item.name || 'approval');
    return [
      {
        type: 'tool_upsert',
        name,
        args: stringifyPayload(item.arguments ?? item.args),
        status: 'paused',
        approvalRequestId: String(item.id || item.approval_request_id || ''),
        previousResponseId: String(data?.response_id || state.currentResponseId || ''),
        serverLabel: String(item.server_label || ''),
      },
    ];
  }

  if (type === 'reasoning' || type === 'reasoning_summary' || type === 'reasoning_summary_text') {
    const text = extractItemText(item);
    return text ? [{ type: 'reasoning_delta', text }] : [];
  }

  if (type === 'message') {
    const text = extractItemText(item);
    return text ? [{ type: status === 'completed' ? 'text_final' : 'text_delta', text }] : [];
  }

  return [];
}

export function normalizeResponsesStreamEvent({ eventName, data, state }) {
  const eventType = String(data?.type || eventName || '').trim();
  const responseId = String(data?.id || data?.response?.id || data?.response_id || '');
  if (responseId.startsWith('resp_')) {
    state.currentResponseId = responseId;
  }

  if (eventType === 'response.tool_call') {
    const name = String(data?.name || data?.tool_name || 'tool');
    const args = stringifyPayload(data?.args ?? data?.arguments);
    return [{ type: 'tool_upsert', name, args, status: 'running' }];
  }

  if (eventType === 'response.tool_result' || eventType === 'response.ksadk.tool_result') {
    const name = String(data?.name || data?.tool_name || 'tool');
    return [{ type: 'tool_result', name, output: stringifyPayload(data?.output ?? data?.result) }];
  }

  if (eventType === 'response.output_item.added') {
    return normalizeOutputItem({ data, state, status: 'running' });
  }

  if (eventType === 'response.output_item.done') {
    return normalizeOutputItem({ data, state, status: 'completed' });
  }

  if (eventType === 'response.function_call_arguments.delta') {
    const key = String(data?.item_id || data?.call_id || '');
    const name = toolNameFor(state, key, data);
    const args = `${state.toolArguments.get(key) || ''}${String(data?.delta || '')}`;
    state.toolArguments.set(key, args);
    return [{ type: 'tool_upsert', name, args, status: 'running' }];
  }

  if (eventType === 'response.function_call_arguments.done') {
    const key = String(data?.item_id || data?.call_id || '');
    const name = toolNameFor(state, key, data);
    const args = stringifyPayload(data?.arguments ?? state.toolArguments.get(key));
    state.toolArguments.set(key, args);
    return [{ type: 'tool_upsert', name, args, status: 'running' }];
  }

  if (
    eventType === 'response.reasoning.delta' ||
    eventType === 'response.reasoning_text.delta' ||
    eventType === 'response.reasoning_summary.delta' ||
    eventType === 'response.reasoning_summary_text.delta'
  ) {
    const text = firstString(data?.delta, data?.text);
    return text ? [{ type: 'reasoning_delta', text }] : [];
  }

  if (eventType === 'response.output_text.delta') {
    const text = firstString(data?.delta, data?.text);
    return text ? [{ type: 'text_delta', text }] : [];
  }

  if (eventType === 'response.output_text.done') {
    const text = firstString(data?.text, data?.delta);
    return text ? [{ type: 'text_final', text }] : [];
  }

  if (eventType === 'response.content_part.delta') {
    const partType = String(data?.part?.type || data?.delta?.type || data?.content_type || '');
    const text = firstString(data?.delta?.text, data?.delta, data?.text);
    if (!text) return [];
    if (partType.includes('reasoning')) {
      return [{ type: 'reasoning_delta', text }];
    }
    return [{ type: 'text_delta', text }];
  }

  if (eventType === 'response.completed') {
    const text = extractCompletedText(data);
    const outputActions = completedOutputItems(data).flatMap((item) => {
      return normalizeOutputItem({
        data: { ...data, item },
        state,
        status: 'completed',
      });
    });
    const hasFinalTextAction = outputActions.some((action) => action.type === 'text_final');
    return [
      ...outputActions,
      ...(text && !hasFinalTextAction ? [{ type: 'text_final', text }] : []),
      { type: 'terminal', status: 'completed' },
    ];
  }

  if (eventType === 'response.failed') {
    return [
      { type: 'failed', message: data?.error?.message || 'Agent 运行失败' },
      { type: 'terminal', status: 'failed' },
    ];
  }

  if (eventType === 'response.incomplete') {
    return [
      { type: 'incomplete' },
      { type: 'terminal', status: 'incomplete' },
    ];
  }

  if (eventType === 'response.approval_request' || eventType === 'response.ksadk.approval_request') {
    const interruptInfo = data?.interrupt_info && typeof data.interrupt_info === 'object' ? data.interrupt_info : {};
    return [
      {
        type: 'approval_request',
        approvalRequestId: String(interruptInfo.approval_request_id || interruptInfo.id || ''),
        previousResponseId: String(data?.response_id || state.currentResponseId || ''),
      },
    ];
  }

  const fallbackText = data?.content?.parts?.[0]?.text;
  if (fallbackText && !data?.actions?.finishReason) {
    return [{ type: 'text_delta', text: String(fallbackText) }];
  }

  return [];
}

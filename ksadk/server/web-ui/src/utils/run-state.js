function parseSeqId(event) {
  const raw = Number(event?.SeqId ?? event?.seq_id ?? event?.seqId ?? 0);
  return Number.isFinite(raw) ? raw : 0;
}

function parseInvocationId(event) {
  return String(event?.InvocationId || event?.invocation_id || event?.invocationId || '').trim();
}

function eventType(event) {
  return String(event?.EventType || event?.event_type || '').trim();
}

function hasAssistantOutputForInvocation(events, invocationId) {
  return events.some((event) => {
    if (parseInvocationId(event) !== invocationId) {
      return false;
    }
    const type = eventType(event);
    return type === 'assistant_message' || type === 'reasoning' || type === 'tool_call';
  });
}

export function findActiveRunIds(events = []) {
  const latestStatusByInvocation = new Map();
  const normalizedEvents = Array.isArray(events) ? events : [];
  for (const event of normalizedEvents) {
    if (eventType(event) !== 'run_status') {
      continue;
    }
    const invocationId = parseInvocationId(event);
    if (!invocationId) {
      continue;
    }
    latestStatusByInvocation.set(invocationId, String(event?.Content?.status || event?.content?.status || '').trim());
  }
  return Array.from(latestStatusByInvocation.entries())
    .filter(([invocationId, status]) => {
      return status === 'in_progress' && !hasAssistantOutputForInvocation(normalizedEvents, invocationId);
    })
    .map(([invocationId]) => invocationId);
}

export function buildSubscribeRunEventsUrl({ sessionId, invocationId, afterSeqId } = {}) {
  const params = new URLSearchParams();
  const normalizedSessionId = String(sessionId || '').trim();
  const normalizedInvocationId = String(invocationId || '').trim();
  if (normalizedSessionId) {
    params.set('SessionId', normalizedSessionId);
  }
  if (normalizedInvocationId) {
    params.set('InvocationId', normalizedInvocationId);
  }
  const seqId = parseSeqId({ SeqId: afterSeqId });
  if (seqId > 0) {
    params.set('AfterSeqId', String(seqId));
  }
  return `/agentengine/api/v1/SubscribeRunEvents?${params.toString()}`;
}

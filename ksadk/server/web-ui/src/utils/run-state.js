function parseSeqId(event) {
  const raw = Number(event?.SeqId ?? event?.seq_id ?? event?.seqId ?? 0);
  return Number.isFinite(raw) ? raw : 0;
}

function parseInvocationId(event) {
  return String(event?.InvocationId || event?.invocation_id || event?.invocationId || '').trim();
}

function parseTimestampMs(event) {
  const raw = event?.Timestamp ?? event?.timestamp ?? event?.created_at ?? event?.CreatedAt;
  if (typeof raw === 'number' && Number.isFinite(raw)) {
    return raw > 1e11 ? raw : raw * 1000;
  }
  if (typeof raw === 'string') {
    const parsed = Date.parse(raw);
    return Number.isNaN(parsed) ? 0 : parsed;
  }
  return 0;
}

function eventType(event) {
  return String(event?.EventType || event?.event_type || '').trim();
}

export function findActiveRunIds(events = [], options = {}) {
  const latestStatusByInvocation = new Map();
  const latestTimestampByInvocation = new Map();
  const normalizedEvents = Array.isArray(events) ? events : [];
  const now = Number.isFinite(Number(options.now)) ? Number(options.now) : Date.now();
  const staleAfterMs = Number.isFinite(Number(options.staleAfterMs))
    ? Number(options.staleAfterMs)
    : 30 * 60 * 1000;
  for (const event of normalizedEvents) {
    const invocationId = parseInvocationId(event);
    if (!invocationId) {
      continue;
    }
    const timestamp = parseTimestampMs(event);
    if (timestamp) {
      latestTimestampByInvocation.set(
        invocationId,
        Math.max(latestTimestampByInvocation.get(invocationId) || 0, timestamp),
      );
    }
    if (eventType(event) !== 'run_status') {
      continue;
    }
    latestStatusByInvocation.set(invocationId, String(event?.Content?.status || event?.content?.status || '').trim());
  }
  return Array.from(latestStatusByInvocation.entries())
    .filter(([invocationId, status]) => {
      const latestTimestamp = latestTimestampByInvocation.get(invocationId) || 0;
      const stale = latestTimestamp > 0 && now - latestTimestamp > staleAfterMs;
      return status === 'in_progress' && !stale;
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

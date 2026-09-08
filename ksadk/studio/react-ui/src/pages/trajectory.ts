export type TrajectoryEvent = {
  projectionVersion: number;
  seqId: number;
  eventId: string;
  recordId: string;
  type: string;
  category: "user" | "context" | "assistant" | "tool" | "approval" | "artifact" | "system";
  turnId: string | null;
  stepId: string | null;
  timestamp: number;
  status: string | null;
  durationMs: number | null;
  summary: string;
  details: Record<string, unknown>;
  source?: Record<string, unknown>;
};

export type TrajectoryRecord = TrajectoryEvent & {
  sourceEvents: TrajectoryEvent[];
  firstSeqId: number;
  lastSeqId: number;
  startedAt: number;
  endedAt: number | null;
};

export type TrajectoryState = {
  items: TrajectoryRecord[];
  bySeq: Map<number, TrajectoryEvent>;
  byRecordId: Map<string, number>;
  lastSeqId: number;
  gap: { afterSeqId: number; beforeSeqId: number } | null;
  hasMore: boolean;
  followTail: boolean;
};

function statusRank(status: string | null): number {
  return status === "failed" || status === "canceled" || status === "cancelled" || status === "interrupted"
    ? 3
    : status === "completed"
      ? 2
      : status === "running"
        ? 1
        : 0;
}

function foldRecord(
  previous: TrajectoryRecord | undefined,
  event: TrajectoryEvent,
): TrajectoryRecord {
  const sourceEvents = [...(previous?.sourceEvents || []), event]
    .sort((left, right) => left.seqId - right.seqId);
  const first = sourceEvents[0];
  const last = sourceEvents[sourceEvents.length - 1];
  const details = { ...(previous?.details || {}), ...event.details };
  const text = event.details.text;
  const isReasoning = event.type.startsWith("reasoning.");
  const isText = event.type.startsWith("text.");
  const isMessage = event.category === "assistant";

  if ((isReasoning || isText) && typeof text === "string") {
    const key = isReasoning ? "reasoning" : "output";
    const prior = previous?.details[key];
    details[key] = event.type.endsWith(".delta") && event.details.replace !== true && typeof prior === "string"
      ? prior + text
      : text;
    details.text = details[key];
  }
  if (event.type === "usage.reported") details.usage = { ...event.details };

  const status = statusRank(event.status) >= statusRank(previous?.status || null)
    ? event.status ?? previous?.status ?? null
    : previous?.status ?? null;
  const ended = Boolean(status && statusRank(status) >= 2)
    || /\.(end|completed|resolved)$/.test(event.type);
  const startedAt = first.timestamp;
  const endedAt = ended ? last.timestamp : previous?.endedAt ?? null;
  const derivedDuration = endedAt !== null && sourceEvents.length > 1 && endedAt > startedAt
    ? (endedAt - startedAt) * 1000
    : null;

  return {
    ...(previous || event),
    ...event,
    type: isMessage ? "assistant.message" : event.type,
    summary: isMessage ? "Message" : event.summary,
    status,
    durationMs: event.durationMs ?? previous?.durationMs ?? derivedDuration,
    details,
    sourceEvents,
    firstSeqId: first.seqId,
    lastSeqId: last.seqId,
    startedAt,
    endedAt,
  };
}

function applyRecord(state: TrajectoryState, event: TrajectoryEvent): void {
  const existing = state.byRecordId.get(event.recordId);
  if (existing === undefined) {
    state.byRecordId.set(event.recordId, state.items.length);
    state.items.push(foldRecord(undefined, event));
    return;
  }
  state.items[existing] = foldRecord(state.items[existing], event);
}

function mutableCopy(state: TrajectoryState): TrajectoryState {
  return {
    ...state,
    items: [...state.items],
    bySeq: new Map(state.bySeq),
    byRecordId: new Map(state.byRecordId),
  };
}

export function trajectoryState(
  events: TrajectoryEvent[] = [],
  hasMore = false,
): TrajectoryState {
  const state: TrajectoryState = {
    items: [],
    bySeq: new Map(),
    byRecordId: new Map(),
    lastSeqId: 0,
    gap: null,
    hasMore,
    followTail: true,
  };
  for (const event of [...events].sort((left, right) => left.seqId - right.seqId)) {
    if (state.bySeq.has(event.seqId)) continue;
    state.bySeq.set(event.seqId, event);
    applyRecord(state, event);
    state.lastSeqId = event.seqId;
  }
  return state;
}

export function mergeTrajectory(
  current: TrajectoryState,
  event: TrajectoryEvent,
  requireContiguous = true,
): TrajectoryState {
  if (current.bySeq.has(event.seqId)) return current;

  const next = mutableCopy(current);
  next.bySeq.set(event.seqId, event);
  if (event.seqId <= next.lastSeqId) return next;
  if (!requireContiguous) {
    applyRecord(next, event);
    next.lastSeqId = event.seqId;
    next.gap = null;
    return next;
  }
  if (event.seqId > next.lastSeqId + 1) {
    next.gap = { afterSeqId: next.lastSeqId, beforeSeqId: event.seqId };
    return next;
  }

  let candidate: TrajectoryEvent | undefined = event;
  while (candidate) {
    applyRecord(next, candidate);
    next.lastSeqId = candidate.seqId;
    candidate = next.bySeq.get(next.lastSeqId + 1);
  }

  const pending = [...next.bySeq.keys()]
    .filter((seqId) => seqId > next.lastSeqId)
    .sort((left, right) => left - right)[0];
  next.gap = pending === undefined
    ? null
    : { afterSeqId: next.lastSeqId, beforeSeqId: pending };
  return next;
}

export function prependTrajectory(
  current: TrajectoryState,
  events: TrajectoryEvent[],
  hasMore: boolean,
): TrajectoryState {
  const bySeq = new Map(current.bySeq);
  for (const event of events) {
    if (!bySeq.has(event.seqId)) bySeq.set(event.seqId, event);
  }
  const next = trajectoryState([...bySeq.values()], hasMore);
  next.followTail = current.followTail;
  return next;
}

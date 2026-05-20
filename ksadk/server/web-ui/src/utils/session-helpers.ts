import type { Session } from '../components/chat/types.js';

export function sessionUpdatedAtValue(session: Session): number {
  const raw = session.UpdatedAt;
  if (typeof raw === 'string') {
    const parsed = Date.parse(raw);
    return Number.isNaN(parsed) ? 0 : parsed;
  }
  if (typeof raw === 'number') {
    return raw;
  }
  return 0;
}

export function upsertSessions(current: Session[], incoming: Session[]): Session[] {
  const merged = new Map<string, Session>();
  for (const session of current) {
    if (!session?.SessionId) continue;
    merged.set(session.SessionId, session);
  }
  for (const session of incoming) {
    if (!session?.SessionId) continue;
    merged.set(session.SessionId, { ...(merged.get(session.SessionId) || {}), ...session });
  }
  return Array.from(merged.values()).sort(
    (left, right) => sessionUpdatedAtValue(right) - sessionUpdatedAtValue(left),
  );
}

export function sessionTitle(session: Session): string {
  const title = String(session.Title || '').trim();
  if (title) {
    return title;
  }
  const firstPrompt = String(session.FirstPrompt || '').trim();
  if (firstPrompt) {
    return firstPrompt;
  }
  return '新对话';
}

export function formatDate(ts?: string | number | null) {
  if (!ts) return '';
  if (typeof ts === 'string') {
    const parsed = Date.parse(ts);
    return Number.isNaN(parsed)
      ? ''
      : new Date(parsed).toLocaleString('zh-CN', {
          month: 'short',
          day: 'numeric',
          hour: '2-digit',
          minute: '2-digit',
        });
  }
  const date = new Date(ts > 1e11 ? ts : ts * 1000);
  return date.toLocaleString('zh-CN', {
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  });
}
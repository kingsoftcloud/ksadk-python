import { create } from 'zustand';
import type { Session } from '../components/chat/types.js';

type SessionState = {
  sessions: Session[];
  currentSessionId: string | null;
};

type SessionActions = {
  setSessions: (sessions: Session[]) => void;
  setCurrentSessionId: (id: string | null) => void;
  upsertSessions: (incoming: Session[]) => void;
  removeSession: (id: string) => void;
};

function sessionUpdatedAtValue(session: Session): number {
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

function upsertSessions(current: Session[], incoming: Session[]): Session[] {
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

export const useSessionStore = create<SessionState & SessionActions>()((set) => ({
  sessions: [],
  currentSessionId: null,
  setSessions: (sessions) => set({ sessions }),
  setCurrentSessionId: (id) => set({ currentSessionId: id }),
  upsertSessions: (incoming) =>
    set((s) => ({ sessions: upsertSessions(s.sessions, incoming) })),
  removeSession: (id) =>
    set((s) => ({ sessions: s.sessions.filter((session) => session.SessionId !== id) })),
}));
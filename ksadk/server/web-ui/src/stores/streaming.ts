import { create } from 'zustand';

export type RunActivityStatus = 'connecting' | 'running' | 'waiting' | 'stopped' | 'completed' | 'failed';

export type RunActivity = {
  runId?: string;
  source: 'run' | 'restore';
  status: RunActivityStatus;
  phase: string;
  detail?: string;
  startedAt: number;
  lastEventAt: number;
  eventCount: number;
};

type StreamingState = {
  isStreaming: boolean;
  currentRunId: string;
  stopRequested: boolean;
  activity: RunActivity | null;
  sessionActivities: Record<string, RunActivity>;
};

type StreamingActions = {
  setStreaming: (streaming: boolean) => void;
  setSessionStreaming: (sessionId: string | null | undefined, streaming: boolean) => void;
  isSessionStreaming: (sessionId: string | null | undefined) => boolean;
  getSessionActivity: (sessionId: string | null | undefined) => RunActivity | null;
  setCurrentRunId: (runId: string) => void;
  requestStop: () => void;
  beginActivity: (activity: {
    runId?: string;
    source?: RunActivity['source'];
    status?: RunActivityStatus;
    phase: string;
    detail?: string;
  }) => void;
  updateActivity: (activity: {
    sessionId?: string | null;
    status?: RunActivityStatus;
    phase?: string;
    detail?: string;
    countEvent?: boolean;
  }) => void;
  stopActivity: (detail?: string) => void;
  stopSessionActivity: (sessionId?: string | null, detail?: string) => void;
  clearActivity: () => void;
  clearSessionActivity: (sessionId?: string | null) => void;
  resetRun: () => void;
};

export const useStreamingStore = create<StreamingState & StreamingActions>()((set) => ({
  isStreaming: false,
  currentRunId: '',
  stopRequested: false,
  activity: null,
  sessionActivities: {},
  setStreaming: (streaming) => set({ isStreaming: streaming }),
  setSessionStreaming: (sessionId, streaming) => set((state) => {
    const key = String(sessionId || '');
    if (!key) return { isStreaming: streaming };
    if (!streaming) {
      const current = state.sessionActivities[key];
      if (!current) {
        return { isStreaming: Object.values(state.sessionActivities).some((activity) => activity.status !== 'completed' && activity.status !== 'failed' && activity.status !== 'stopped') };
      }
      return {
        isStreaming: Object.entries(state.sessionActivities).some(([id, activity]) => (
          id !== key && activity.status !== 'completed' && activity.status !== 'failed' && activity.status !== 'stopped'
        )),
      };
    }
    return { isStreaming: true };
  }),
  isSessionStreaming: (sessionId) => {
    const activity = useStreamingStore.getState().getSessionActivity(sessionId);
    return Boolean(activity && activity.status !== 'completed' && activity.status !== 'failed' && activity.status !== 'stopped');
  },
  getSessionActivity: (sessionId) => {
    const key = String(sessionId || '');
    if (!key) return null;
    return useStreamingStore.getState().sessionActivities[key] || null;
  },
  setCurrentRunId: (runId) => set({ currentRunId: runId }),
  requestStop: () => set({ stopRequested: true }),
  beginActivity: (activity) => set({
    stopRequested: false,
    activity: {
      runId: activity.runId,
      source: activity.source || 'run',
      status: activity.status || 'running',
      phase: activity.phase,
      detail: activity.detail,
      startedAt: Date.now(),
      lastEventAt: Date.now(),
      eventCount: 0,
    },
  }),
  updateActivity: (activity) => set((state) => {
    const key = String(activity.sessionId || '');
    const current = key ? state.sessionActivities[key] : state.activity;
    const nextActivity = current
      ? {
          ...current,
          status: activity.status || current.status,
          phase: activity.phase || current.phase,
          detail: activity.detail === undefined ? current.detail : activity.detail,
          lastEventAt: Date.now(),
          eventCount: current.eventCount + (activity.countEvent === false ? 0 : 1),
        }
      : {
          source: 'run' as const,
          status: activity.status || 'running',
          phase: activity.phase || '正在运行',
          detail: activity.detail,
          startedAt: Date.now(),
          lastEventAt: Date.now(),
          eventCount: activity.countEvent === false ? 0 : 1,
        };
    if (key) {
      return {
        isStreaming: nextActivity.status !== 'completed' && nextActivity.status !== 'failed' && nextActivity.status !== 'stopped'
          ? true
          : Object.entries(state.sessionActivities).some(([id, existing]) => (
              id !== key && existing.status !== 'completed' && existing.status !== 'failed' && existing.status !== 'stopped'
            )),
        sessionActivities: {
          ...state.sessionActivities,
          [key]: nextActivity,
        },
        activity: nextActivity,
      };
    }
    if (!current) {
      return {
        activity: nextActivity,
      };
    }
    return {
      activity: nextActivity,
    };
  }),
  stopActivity: (detail) => set((state) => ({
    isStreaming: false,
    stopRequested: true,
    activity: state.activity
      ? {
        ...state.activity,
        status: 'stopped',
        phase: '已断开输出流',
        detail: detail || '前端已断开本次输出流；后台运行可稍后通过会话记录继续查看。',
        lastEventAt: Date.now(),
      }
      : null,
  })),
  stopSessionActivity: (sessionId, detail) => set((state) => {
    const key = String(sessionId || '');
    if (!key) return {};
    const current = state.sessionActivities[key];
    if (!current) return {};
    const nextActivity = {
      ...current,
      status: 'stopped' as const,
      phase: '已断开输出流',
      detail: detail || '前端已断开本次输出流；后台运行可稍后通过会话记录继续查看。',
      lastEventAt: Date.now(),
    };
    const sessionActivities = { ...state.sessionActivities, [key]: nextActivity };
    return {
      isStreaming: Object.entries(sessionActivities).some(([id, activity]) => (
        id !== key && activity.status !== 'completed' && activity.status !== 'failed' && activity.status !== 'stopped'
      )),
      activity: nextActivity,
      sessionActivities,
    };
  }),
  clearActivity: () => set({ activity: null }),
  clearSessionActivity: (sessionId) => set((state) => {
    const key = String(sessionId || '');
    if (!key) return {};
    const { [key]: _removed, ...sessionActivities } = state.sessionActivities;
    return {
      sessionActivities,
      isStreaming: Object.values(sessionActivities).some((activity) => activity.status !== 'completed' && activity.status !== 'failed' && activity.status !== 'stopped'),
      activity: state.activity === _removed ? null : state.activity,
    };
  }),
  resetRun: () => set({ isStreaming: false, currentRunId: '', stopRequested: false, activity: null, sessionActivities: {} }),
}));

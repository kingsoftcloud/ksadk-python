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
};

type StreamingActions = {
  setStreaming: (streaming: boolean) => void;
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
    status?: RunActivityStatus;
    phase?: string;
    detail?: string;
    countEvent?: boolean;
  }) => void;
  stopActivity: (detail?: string) => void;
  clearActivity: () => void;
  resetRun: () => void;
};

export const useStreamingStore = create<StreamingState & StreamingActions>()((set) => ({
  isStreaming: false,
  currentRunId: '',
  stopRequested: false,
  activity: null,
  setStreaming: (streaming) => set({ isStreaming: streaming }),
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
    const current = state.activity;
    if (!current) {
      return {
        activity: {
          source: 'run',
          status: activity.status || 'running',
          phase: activity.phase || '正在运行',
          detail: activity.detail,
          startedAt: Date.now(),
          lastEventAt: Date.now(),
          eventCount: activity.countEvent === false ? 0 : 1,
        },
      };
    }
    return {
      activity: {
        ...current,
        status: activity.status || current.status,
        phase: activity.phase || current.phase,
        detail: activity.detail === undefined ? current.detail : activity.detail,
        lastEventAt: Date.now(),
        eventCount: current.eventCount + (activity.countEvent === false ? 0 : 1),
      },
    };
  }),
  stopActivity: (detail) => set((state) => ({
    isStreaming: false,
    stopRequested: true,
    activity: state.activity
      ? {
          ...state.activity,
          status: 'stopped',
          phase: '已停止接收',
          detail: detail || '前端已断开本次输出流；如果运行时不支持取消，后台可能仍在继续。',
          lastEventAt: Date.now(),
        }
      : null,
  })),
  clearActivity: () => set({ activity: null }),
  resetRun: () => set({ isStreaming: false, currentRunId: '', stopRequested: false, activity: null }),
}));

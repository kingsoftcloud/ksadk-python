import { create } from 'zustand';

type StreamingState = {
  isStreaming: boolean;
  currentRunId: string;
  stopRequested: boolean;
};

type StreamingActions = {
  setStreaming: (streaming: boolean) => void;
  setCurrentRunId: (runId: string) => void;
  requestStop: () => void;
  resetRun: () => void;
};

export const useStreamingStore = create<StreamingState & StreamingActions>()((set) => ({
  isStreaming: false,
  currentRunId: '',
  stopRequested: false,
  setStreaming: (streaming) => set({ isStreaming: streaming }),
  setCurrentRunId: (runId) => set({ currentRunId: runId }),
  requestStop: () => set({ stopRequested: true }),
  resetRun: () => set({ isStreaming: false, currentRunId: '', stopRequested: false }),
}));
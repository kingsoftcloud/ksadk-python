import { create } from 'zustand';

type WorkspaceFile = {
  name: string;
  path: string;
  type: 'file' | 'directory';
  size?: number;
  modifiedAt?: string;
};

type WorkspaceState = {
  files: WorkspaceFile[];
  currentPath: string;
  initialized: boolean;
};

type WorkspaceActions = {
  setFiles: (files: WorkspaceFile[]) => void;
  setCurrentPath: (path: string) => void;
  setInitialized: (initialized: boolean) => void;
};

export const useWorkspaceStore = create<WorkspaceState & WorkspaceActions>()((set) => ({
  files: [],
  currentPath: '.',
  initialized: false,
  setFiles: (files) => set({ files }),
  setCurrentPath: (path) => set({ currentPath: path }),
  setInitialized: (initialized) => set({ initialized }),
}));
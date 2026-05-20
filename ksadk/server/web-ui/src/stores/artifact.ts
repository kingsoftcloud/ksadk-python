import { create } from 'zustand';

type ArtifactState = {
  content: string | null;
  type: string;
  visible: boolean;
};

type ArtifactActions = {
  show: (content: string, type?: string) => void;
  hide: () => void;
  setContent: (content: string | null, type?: string) => void;
};

export const useArtifactStore = create<ArtifactState & ArtifactActions>()((set) => ({
  content: null,
  type: 'html',
  visible: false,
  show: (content, type = 'html') => set({ content, type, visible: true }),
  hide: () => set({ visible: false }),
  setContent: (content, type) =>
    set((s) => ({ content, type: type ?? s.type })),
}));
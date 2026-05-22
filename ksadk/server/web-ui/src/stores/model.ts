import { create } from 'zustand';
import type { ModelCatalogItem } from '../components/chat/types';

export type ThinkingMode = 'auto' | 'enabled' | 'disabled';

type ModelState = {
  availableModels: ModelCatalogItem[];
  selectedModel: string;
  modelSource: string;
  modelCatalogLoaded: boolean;
  thinkingMode: ThinkingMode;
};

type ModelActions = {
  upsertModels: (incoming: ModelCatalogItem[]) => void;
  setSelectedModel: (id: string) => void;
  setModelSource: (source: string) => void;
  setModelCatalogLoaded: (loaded: boolean) => void;
  setThinkingMode: (mode: ThinkingMode) => void;
};

function upsertModelOptions(current: ModelCatalogItem[], incoming: ModelCatalogItem[]): ModelCatalogItem[] {
  const merged = new Map<string, ModelCatalogItem>();
  for (const item of current) {
    if (!item?.id) continue;
    merged.set(item.id, item);
  }
  for (const item of incoming) {
    if (!item?.id) continue;
    merged.set(item.id, { ...(merged.get(item.id) || {}), ...item });
  }
  return Array.from(merged.values()).sort((a, b) => a.id.localeCompare(b.id));
}

export const useModelStore = create<ModelState & ModelActions>()((set) => ({
  availableModels: [],
  selectedModel: '',
  modelSource: '',
  modelCatalogLoaded: false,
  thinkingMode: 'auto',
  upsertModels: (incoming) =>
    set((s) => ({ availableModels: upsertModelOptions(s.availableModels, incoming) })),
  setSelectedModel: (id) => set({ selectedModel: id }),
  setModelSource: (source) => set({ modelSource: source }),
  setModelCatalogLoaded: (loaded) => set({ modelCatalogLoaded: loaded }),
  setThinkingMode: (mode) => set({ thinkingMode: mode }),
}));

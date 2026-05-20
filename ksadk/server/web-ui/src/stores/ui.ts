import { create } from 'zustand';
import type { MessageAttachment, PreviewImageSize } from '../components/chat/types.js';

type ToastEntry = {
  id: string;
  message: string;
  variant: 'error' | 'info';
  createdAt: number;
};

type UIState = {
  sidebarOpen: boolean;
  mobileSidebarOpen: boolean;
  mobileActionsOpen: boolean;
  workspacePanelOpen: boolean;
  workspacePanelWidth: number;
  workspacePanelFullscreen: boolean;
  input: string;
  attachments: File[];
  previewAttachment: MessageAttachment | null;
  previewImageSize: PreviewImageSize | null;
  queuedDrafts: Array<{ text: string; attachments: File[] }>;
  toasts: ToastEntry[];
};

type UIActions = {
  toggleSidebar: () => void;
  setSidebarOpen: (open: boolean) => void;
  toggleMobileSidebar: () => void;
  setMobileSidebarOpen: (open: boolean) => void;
  toggleMobileActions: () => void;
  setMobileActionsOpen: (open: boolean) => void;
  setWorkspacePanelOpen: (open: boolean) => void;
  setWorkspacePanelWidth: (width: number) => void;
  setWorkspacePanelFullscreen: (fullscreen: boolean) => void;
  setInput: (input: string) => void;
  setAttachments: (updater: File[] | ((prev: File[]) => File[])) => void;
  setPreviewAttachment: (attachment: MessageAttachment | null) => void;
  setPreviewImageSize: (size: PreviewImageSize | null) => void;
  setQueuedDrafts: (updater: Array<{ text: string; attachments: File[] }> | ((prev: Array<{ text: string; attachments: File[] }>) => Array<{ text: string; attachments: File[] }>)) => void;
  pushToast: (message: string, variant?: 'error' | 'info') => void;
  dismissToast: (id: string) => void;
};

function applyUpdater<T>(value: T, updater: T | ((prev: T) => T)): T {
  return typeof updater === 'function' ? (updater as (prev: T) => T)(value) : updater;
}

export const useUIStore = create<UIState & UIActions>()((set) => ({
  sidebarOpen: true,
  mobileSidebarOpen: false,
  mobileActionsOpen: false,
  workspacePanelOpen: false,
  workspacePanelWidth: 820,
  workspacePanelFullscreen: false,
  input: '',
  attachments: [],
  previewAttachment: null,
  previewImageSize: null,
  queuedDrafts: [],
  toasts: [],
  toggleSidebar: () => set((s) => ({ sidebarOpen: !s.sidebarOpen })),
  setSidebarOpen: (open) => set({ sidebarOpen: open }),
  toggleMobileSidebar: () => set((s) => ({ mobileSidebarOpen: !s.mobileSidebarOpen })),
  setMobileSidebarOpen: (open) => set({ mobileSidebarOpen: open }),
  toggleMobileActions: () => set((s) => ({ mobileActionsOpen: !s.mobileActionsOpen })),
  setMobileActionsOpen: (open) => set({ mobileActionsOpen: open }),
  setWorkspacePanelOpen: (open) => set({ workspacePanelOpen: open }),
  setWorkspacePanelWidth: (width) => set({ workspacePanelWidth: width }),
  setWorkspacePanelFullscreen: (fullscreen) => set({ workspacePanelFullscreen: fullscreen }),
  setInput: (input) => set({ input }),
  setAttachments: (updater) =>
    set((s) => ({ attachments: applyUpdater(s.attachments, updater) })),
  setPreviewAttachment: (attachment) => set({ previewAttachment: attachment }),
  setPreviewImageSize: (size) => set({ previewImageSize: size }),
  setQueuedDrafts: (updater) =>
    set((s) => ({ queuedDrafts: applyUpdater(s.queuedDrafts, updater) })),
  pushToast: (message, variant = 'info') =>
    set((s) => ({
      toasts: [
        ...s.toasts,
        { id: String(Date.now() + Math.random()), message, variant, createdAt: Date.now() },
      ],
    })),
  dismissToast: (id) =>
    set((s) => ({ toasts: s.toasts.filter((t) => t.id !== id) })),
}));
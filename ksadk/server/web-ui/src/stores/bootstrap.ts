import { create } from 'zustand';
import { normalizeCapabilities } from '../utils/capabilities.js';
import type { RuntimeApiFormat } from '../types/api.js';
import type { UiCapabilities } from '../types/capabilities.js';

type BootstrapState = {
  agentId: string;
  agentName: string;
  agentFramework: string;
  capabilities: UiCapabilities;
  apiFormats: RuntimeApiFormat[];
  accessMode: string;
  workspaceFiles: Record<string, unknown> | null;
};

type BootstrapActions = {
  setAgentId: (id: string) => void;
  setAgentName: (name: string) => void;
  setAgentFramework: (framework: string) => void;
  setCapabilities: (caps: UiCapabilities) => void;
  setApiFormats: (formats: RuntimeApiFormat[]) => void;
  setAccessMode: (mode: string) => void;
  setWorkspaceFiles: (files: Record<string, unknown> | null) => void;
};

export const useBootstrapStore = create<BootstrapState & BootstrapActions>()((set) => ({
  agentId: 'default-agent',
  agentName: 'Agent',
  agentFramework: '',
  capabilities: normalizeCapabilities({}),
  apiFormats: ['responses', 'chat_completions'],
  accessMode: 'Owner',
  workspaceFiles: null,
  setAgentId: (id) => set({ agentId: id }),
  setAgentName: (name) => set({ agentName: name }),
  setAgentFramework: (framework) => set({ agentFramework: framework }),
  setCapabilities: (caps) => set({ capabilities: caps }),
  setApiFormats: (formats) => set({ apiFormats: formats }),
  setAccessMode: (mode) => set({ accessMode: mode }),
  setWorkspaceFiles: (files) => set({ workspaceFiles: files }),
}));

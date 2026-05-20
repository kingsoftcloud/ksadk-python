export type CapabilitySlot =
  | 'sidebar' | 'header-actions' | 'panel-right' | 'launcher' | 'overlay';

export type CapabilityContext = {
  api: import('../api/types.js').ApiFacade;
  agentId: string;
  isMobile: boolean;
  isStreaming: boolean;
};

export interface CapabilityPlugin {
  id: string;
  isEnabled(capabilities: import('../../types/capabilities.js').UiCapabilities): boolean;
  getComponent(slot: CapabilitySlot, context: CapabilityContext): React.ComponentType | null;
}

import type { CapabilityPlugin, CapabilitySlot, CapabilityContext } from '../core/capability/types.js';
import type { UiCapabilities } from '../types/capabilities.js';
import { WorkspacePanel } from '../components/workspace/WorkspacePanel.js';
import type { WorkspaceFilesCapability } from '../components/chat/types.js';

const DEFAULT_WORKSPACE_CAPABILITY: WorkspaceFilesCapability = {
  Enabled: true,
  MaxUploadBytes: 100 * 1024 * 1024,
  SupportsDelete: true,
  RootLabel: 'Workspace',
};

export const workspacePlugin: CapabilityPlugin = {
  id: 'WorkspaceFiles',

  isEnabled(capabilities: UiCapabilities): boolean {
    return Boolean(capabilities.WorkspaceFiles);
  },

  getComponent(slot: CapabilitySlot, context: CapabilityContext): React.ComponentType | null {
    if (slot === 'panel-right') {
      return () => (
        <WorkspacePanel
          agentId={context.agentId}
          capability={DEFAULT_WORKSPACE_CAPABILITY}
          open={true}
          isMobile={context.isMobile}
          api={context.api}
        />
      );
    }
    return null;
  },
};

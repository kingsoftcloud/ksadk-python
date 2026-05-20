import type { CapabilityPlugin, CapabilitySlot, CapabilityContext } from '../core/capability/types.js';
import type { UiCapabilities } from '../types/capabilities.js';
import { WorkspacePanel } from '../components/workspace/WorkspacePanel.js';

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
          capability={{} as any}
          open={true}
          isMobile={context.isMobile}
          api={context.api}
        />
      );
    }
    return null;
  },
};

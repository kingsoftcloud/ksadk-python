import type { CapabilityPlugin, CapabilitySlot, CapabilityContext } from '../core/capability/types.js';
import type { UiCapabilities } from '../types/capabilities.js';
import { NativeTerminalPanel } from '../components/native/NativeTerminalPanel.js';

export const terminalPlugin: CapabilityPlugin = {
  id: 'NativeTerminal',

  isEnabled(capabilities: UiCapabilities): boolean {
    return capabilities.NativeTerminal?.Enabled ?? false;
  },

  getComponent(slot: CapabilitySlot, context: CapabilityContext): React.ComponentType | null {
    if (slot === 'overlay') {
      return () => (
        <NativeTerminalPanel
          agentId={context.agentId}
          isMobile={context.isMobile}
        />
      );
    }
    return null;
  },
};

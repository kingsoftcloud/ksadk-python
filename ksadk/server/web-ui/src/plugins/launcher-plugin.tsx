import type { CapabilityPlugin, CapabilitySlot, CapabilityContext } from '../core/capability/types.js';
import type { UiCapabilities } from '../types/capabilities.js';
import { NativeRuntimeLauncher } from '../components/native/NativeRuntimeLauncher.js';
import { isHostedChatEnabled } from '../utils/capabilities.js';

export const launcherPlugin: CapabilityPlugin = {
  id: 'launcher',

  isEnabled(capabilities: UiCapabilities): boolean {
    return !isHostedChatEnabled(capabilities);
  },

  getComponent(slot: CapabilitySlot, context: CapabilityContext): React.ComponentType | null {
    if (slot === 'launcher') {
      return () => (
        <NativeRuntimeLauncher
          productLabel="原生运行时"
          nativeManagementLink={null}
          nativeTerminal={undefined}
          workspaceEnabled={false}
          onOpenWorkspace={() => {}}
        />
      );
    }
    return null;
  },
};

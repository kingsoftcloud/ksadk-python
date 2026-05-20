import { lazy, Suspense } from 'react';
import type { CapabilityPlugin, CapabilitySlot } from '../core/capability/types.js';
import type { UiCapabilities } from '../types/capabilities.js';

const lazyNativeTerminalPanel = lazy(() =>
  import('../components/native/NativeTerminalPanel.js').then((module) => ({
    default: module.NativeTerminalPanel,
  })),
);

export const terminalPlugin: CapabilityPlugin = {
  id: 'NativeTerminal',

  isEnabled(capabilities: UiCapabilities): boolean {
    return capabilities.NativeTerminal?.Enabled ?? false;
  },

  getComponent(slot: CapabilitySlot): React.ComponentType | null {
    if (slot === 'overlay') {
      const NativeTerminalPanel = lazyNativeTerminalPanel;
      return () => (
        <Suspense fallback={null}>
          <NativeTerminalPanel
            capability={{
              Enabled: true,
              Mode: 'tui',
              Path: '/_ksadk/terminal/ws',
              Protocol: 'ks-terminal.v1',
            }}
            open={true}
            onClose={() => {}}
          />
        </Suspense>
      );
    }
    return null;
  },
};

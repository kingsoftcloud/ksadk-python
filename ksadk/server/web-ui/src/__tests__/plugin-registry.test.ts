import { describe, it, expect } from 'vitest';
import { PluginRegistry } from '../core/capability/registry.js';
import type { CapabilityPlugin } from '../core/capability/types.js';
import type { UiCapabilities } from '../types/capabilities.js';
import type { ApiFacade } from '../core/api/types.js';

const mockCapabilities: UiCapabilities = {
  HostedChat: { Enabled: true, ApiFormats: ['responses'] },
  NativeDashboard: { Enabled: false },
  NativeTerminal: { Enabled: true, Mode: 'tui', Protocol: 'ks-terminal.v1', Path: '/_ksadk/terminal/ws' },
  RunLifecycle: { Enabled: true, Resume: true, Abort: true },
  WorkspaceFiles: true,
  Thinking: true,
};

const alwaysOnPlugin: CapabilityPlugin = {
  id: 'always-on',
  isEnabled: () => true,
  getComponent: () => null,
};

const alwaysOffPlugin: CapabilityPlugin = {
  id: 'always-off',
  isEnabled: () => false,
  getComponent: () => null,
};

const terminalPlugin: CapabilityPlugin = {
  id: 'terminal',
  isEnabled: (caps) => caps.NativeTerminal?.Enabled ?? false,
  getComponent: () => null,
};

describe('PluginRegistry', () => {
  it('registers and queries plugins', () => {
    const registry = new PluginRegistry();
    registry.register(alwaysOnPlugin);
    registry.register(alwaysOffPlugin);
    registry.register(terminalPlugin);

    const enabled = registry.getEnabled(mockCapabilities);
    expect(enabled).toHaveLength(2);
    expect(enabled.map((p) => p.id)).toContain('always-on');
    expect(enabled.map((p) => p.id)).toContain('terminal');
  });

  it('returns empty array when no plugins', () => {
    const registry = new PluginRegistry();
    expect(registry.getEnabled(mockCapabilities)).toHaveLength(0);
  });

  it('getForSlot filters by slot and capability', () => {
    const registry = new PluginRegistry();
    registry.register(alwaysOnPlugin);
    registry.register(terminalPlugin);

    const components = registry.getForSlot('overlay', mockCapabilities, {
      api: {} as ApiFacade,
      agentId: 'test',
      isMobile: false,
      isStreaming: false,
    });
    // Both plugins return null from getComponent for 'overlay' slot
    expect(components).toHaveLength(0);
  });
});

import type { CapabilityPlugin, CapabilitySlot, CapabilityContext } from './types.js';
import type { UiCapabilities } from '../../types/capabilities.js';

export class PluginRegistry {
  private plugins: CapabilityPlugin[] = [];

  register(plugin: CapabilityPlugin): void {
    this.plugins.push(plugin);
  }

  getEnabled(capabilities: UiCapabilities): CapabilityPlugin[] {
    return this.plugins.filter((p) => p.isEnabled(capabilities));
  }

  getForSlot(
    slot: CapabilitySlot,
    capabilities: UiCapabilities,
    context: CapabilityContext,
  ): React.ComponentType[] {
    return this.plugins
      .filter((p) => p.isEnabled(capabilities))
      .map((p) => p.getComponent(slot, context))
      .filter((c): c is React.ComponentType => c !== null);
  }
}

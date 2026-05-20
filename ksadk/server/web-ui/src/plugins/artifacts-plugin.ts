import type { CapabilityPlugin, CapabilitySlot, CapabilityContext } from '../core/capability/types.js';
import type { UiCapabilities } from '../types/capabilities.js';
import React from 'react';
import { ArtifactsPanel } from '../components/artifacts/ArtifactsPanel.js';

export const artifactsPlugin: CapabilityPlugin = {
  id: 'artifacts',

  isEnabled(): boolean {
    return true;
  },

  getComponent(slot: CapabilitySlot, _context: CapabilityContext): React.ComponentType | null {
    if (slot === 'panel-right') {
      return ArtifactsPanel;
    }
    return null;
  },
};

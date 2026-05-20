import type { CapabilityPlugin, CapabilitySlot } from '../core/capability/types.js';
import React from 'react';
import { ArtifactsPanel } from '../components/artifacts/ArtifactsPanel.js';

export const artifactsPlugin: CapabilityPlugin = {
  id: 'artifacts',

  isEnabled(): boolean {
    return true;
  },

  getComponent(slot: CapabilitySlot): React.ComponentType | null {
    if (slot === 'panel-right') {
      return ArtifactsPanel;
    }
    return null;
  },
};

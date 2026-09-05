import type { ComponentType } from "react";
import { useSyncExternalStore } from "react";
import type { DshUiSessionCreateResponse } from "./dshUiSandbox";

export const STUDIO_DSH_SLOTS = {
  action: "studio.action",
  agentProvider: "studio.agent.provider",
  renderer: "studio.renderer",
  route: "studio.route",
  settingsPage: "studio.settings.page",
  sidebarNavigation: "studio.sidebar.navigation",
  workspaceTab: "studio.workspace.tab",
} as const;

export type StudioDshSlotName = (typeof STUDIO_DSH_SLOTS)[keyof typeof STUDIO_DSH_SLOTS];

export interface StudioContributionBase {
  id: string;
  order?: number;
}

export interface StudioSidebarNavigationContribution extends StudioContributionBase {
  label: string;
  path: string;
}

export interface StudioRouteContribution extends StudioContributionBase {
  path: string;
  title: string;
  workspaceTabId: string;
}

export interface StudioWorkspaceTabProps {
  active: boolean;
  currentAgentId: string;
  path: string;
}

export interface StudioWorkspaceTabRendererSandboxedIframe {
  type: "sandboxed-iframe";
  frameUrl: string;
}

export interface StudioWorkspaceTabContribution extends StudioContributionBase {
  component?: ComponentType<StudioWorkspaceTabProps>;
  label: string;
  /** Sandbox renderer declared by the backend; takes precedence over component. */
  renderer?: StudioWorkspaceTabRendererSandboxedIframe;
  /** Live UI session payload backing the sandboxed-iframe renderer. */
  session?: DshUiSessionCreateResponse;
}

/**
 * A DSH plugin contributes an AgentProvider descriptor rather than inventing a
 * second Studio manifest.  Runtime remains the existing `plugin` type and the
 * immutable providerRef identifies the selected DSH-owned implementation.
 */
export interface StudioAgentProviderContribution extends StudioContributionBase {
  compatible: boolean;
  description?: string;
  displayName: string;
  providerRef: string;
  /** Runtime health, not install-manifest state. Only ready providers are selectable. */
  state: "ready" | "degraded" | "disabled";
}

export interface StudioActionContribution extends StudioContributionBase {
  label: string;
  path?: string;
  run?: () => void | Promise<void>;
}

export interface StudioSettingsPageContribution extends StudioContributionBase {
  component: ComponentType;
  label: string;
  path: string;
}

export interface StudioRendererProps {
  payload: unknown;
}

export interface StudioRendererContribution extends StudioContributionBase {
  component: ComponentType<StudioRendererProps>;
  kind: string;
}

export interface StudioDshContributionMap {
  [STUDIO_DSH_SLOTS.action]: StudioActionContribution;
  [STUDIO_DSH_SLOTS.agentProvider]: StudioAgentProviderContribution;
  [STUDIO_DSH_SLOTS.renderer]: StudioRendererContribution;
  [STUDIO_DSH_SLOTS.route]: StudioRouteContribution;
  [STUDIO_DSH_SLOTS.settingsPage]: StudioSettingsPageContribution;
  [STUDIO_DSH_SLOTS.sidebarNavigation]: StudioSidebarNavigationContribution;
  [STUDIO_DSH_SLOTS.workspaceTab]: StudioWorkspaceTabContribution;
}

type Listener = () => void;

export class StudioContributionRegistry {
  private entries = new Map<StudioDshSlotName, Map<string, StudioDshContributionMap[StudioDshSlotName]>>();
  private listeners = new Map<StudioDshSlotName, Set<Listener>>();
  private snapshots = new Map<StudioDshSlotName, readonly StudioDshContributionMap[StudioDshSlotName][]>();

  register<K extends StudioDshSlotName>(slot: K, contribution: StudioDshContributionMap[K]): () => void {
    const slotEntries = this.entries.get(slot) ?? new Map();
    if (slotEntries.has(contribution.id)) {
      throw new Error(`DSH contribution already registered: ${slot}/${contribution.id}`);
    }
    slotEntries.set(contribution.id, contribution);
    this.entries.set(slot, slotEntries);
    this.publish(slot);
    let active = true;
    return () => {
      if (!active) return;
      active = false;
      slotEntries.delete(contribution.id);
      this.publish(slot);
    };
  }

  /** Replace one complete Cordis graph and notify each affected slot once. */
  replaceAll(source: StudioContributionRegistry): void {
    const nextEntries = source.cloneEntries();
    const changed = new Set<StudioDshSlotName>([
      ...this.entries.keys(),
      ...nextEntries.keys(),
    ]);
    this.entries = nextEntries;
    for (const slot of changed) this.publish(slot);
  }

  getEntries<K extends StudioDshSlotName>(slot: K): readonly StudioDshContributionMap[K][] {
    const existing = this.snapshots.get(slot);
    if (existing) return existing as readonly StudioDshContributionMap[K][];
    const snapshot = this.createSnapshot(slot);
    this.snapshots.set(slot, snapshot);
    return snapshot as readonly StudioDshContributionMap[K][];
  }

  subscribe(slot: StudioDshSlotName, listener: Listener): () => void {
    const slotListeners = this.listeners.get(slot) ?? new Set();
    slotListeners.add(listener);
    this.listeners.set(slot, slotListeners);
    return () => slotListeners.delete(listener);
  }

  private cloneEntries(): Map<StudioDshSlotName, Map<string, StudioDshContributionMap[StudioDshSlotName]>> {
    return new Map(
      [...this.entries].map(([slot, entries]) => [slot, new Map(entries)]),
    );
  }

  private createSnapshot(slot: StudioDshSlotName): readonly StudioDshContributionMap[StudioDshSlotName][] {
    return Object.freeze(
      [...(this.entries.get(slot)?.values() ?? [])]
        .sort((left, right) => (left.order ?? 100) - (right.order ?? 100) || left.id.localeCompare(right.id)),
    );
  }

  private publish(slot: StudioDshSlotName): void {
    this.snapshots.set(slot, this.createSnapshot(slot));
    for (const listener of this.listeners.get(slot) ?? []) listener();
  }
}

export function useStudioDshContributions<K extends StudioDshSlotName>(
  registry: StudioContributionRegistry,
  slot: K,
): readonly StudioDshContributionMap[K][] {
  return useSyncExternalStore(
    listener => registry.subscribe(slot, listener),
    () => registry.getEntries(slot),
    () => registry.getEntries(slot),
  );
}

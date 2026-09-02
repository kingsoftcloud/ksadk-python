import * as React from "react";
import { apiFetch } from "../api";
import { StudioContributionRegistry } from "./studioContributions";
import type { DshClientPlugin } from "./studioDshRuntime";
import { StudioDshRuntime, studioDshRuntime } from "./studioDshRuntime";

export interface StudioDshClientBundleProjection {
  compatible: boolean;
  contentBytes: number;
  digest: string;
  enabled: boolean;
  external: string[];
  incompatibilityReason?: string | null;
  inject: string[];
  pluginId: string;
  url: string;
}

export interface StudioDshProfileProjection {
  clientBundles: StudioDshClientBundleProjection[];
  clientGraphDigest: string;
}

interface ClientBundleRegistration {
  factory(require: (id: string) => unknown): unknown;
  id: string;
}

interface ModuleLoaderTarget {
  load(registration: ClientBundleRegistration): void;
}

type BundleLoader = (bundle: StudioDshClientBundleProjection) => Promise<DshClientPlugin>;

declare global {
  interface Window {
    __ModuleLoader__?: ModuleLoaderTarget;
  }
}

function isClientPlugin(value: unknown): value is DshClientPlugin {
  return typeof value === "object" && value !== null && typeof (value as DshClientPlugin).apply === "function";
}

/** Execute one immutable DSH client artifact through its canonical ModuleLoader handoff. */
export async function loadDshClientBundle(
  bundle: StudioDshClientBundleProjection,
): Promise<DshClientPlugin> {
  const previous = window.__ModuleLoader__;
  let registration: ClientBundleRegistration | undefined;
  window.__ModuleLoader__ = {
    load(next) {
      if (registration) throw new Error(`DSH client bundle ${bundle.pluginId} registered more than once`);
      registration = next;
    },
  };
  try {
    await new Promise<void>((resolve, reject) => {
      const script = document.createElement("script");
      script.async = true;
      script.src = bundle.url;
      script.onload = () => { script.remove(); resolve(); };
      script.onerror = () => {
        script.remove();
        reject(new Error(`DSH client bundle ${bundle.pluginId} failed to load`));
      };
      document.head.append(script);
    });
  } finally {
    if (previous) window.__ModuleLoader__ = previous;
    else delete window.__ModuleLoader__;
  }
  if (!registration || registration.id !== bundle.pluginId) {
    throw new Error(`DSH client bundle ${bundle.pluginId} registered an unexpected module`);
  }
  const modules: Record<string, unknown> = { react: React };
  const plugin = registration.factory(id => {
    if (!(id in modules)) throw new Error(`DSH client bundle ${bundle.pluginId} requires unavailable module ${id}`);
    return modules[id];
  });
  if (!isClientPlugin(plugin)) {
    throw new Error(`DSH client bundle ${bundle.pluginId} did not export a Cordis plugin`);
  }
  return plugin;
}

/**
 * Own the installed Profile's browser graph. New bundles mount in an isolated
 * Cordis root and become visible only after the whole graph succeeds.
 */
export class StudioDshCompositionHost {
  private activeDigest = "";
  private activeRuntime: StudioDshRuntime | null = null;
  private pending: Promise<void> = Promise.resolve();

  constructor(
    private readonly target: StudioDshRuntime,
    private readonly bundleLoader: BundleLoader = loadDshClientBundle,
  ) {}

  refresh(): Promise<void> {
    return this.enqueue(async () => {
      const response = await apiFetch("/api/v1/plugin-ecosystems/dsh/profile");
      if (!response.ok) throw new Error("DSH Profile client graph is unavailable");
      await this.activate(await response.json() as StudioDshProfileProjection);
    });
  }

  reconcile(projection: StudioDshProfileProjection): Promise<void> {
    return this.enqueue(() => this.activate(projection));
  }

  private async activate(projection: StudioDshProfileProjection): Promise<void> {
    if (projection.clientGraphDigest === this.activeDigest) return;
    const active = projection.clientBundles.filter(bundle => bundle.enabled && bundle.compatible);
    if (active.length !== projection.clientBundles.filter(bundle => bundle.enabled).length) {
      const incompatible = projection.clientBundles.find(bundle => bundle.enabled && !bundle.compatible);
      throw new Error(
        `DSH client bundle ${incompatible?.pluginId || "unknown"} is incompatible: ${incompatible?.incompatibilityReason || "unknown reason"}`,
      );
    }

    const staging = new StudioDshRuntime();
    try {
      for (const bundle of active) {
        const plugin = await this.bundleLoader(bundle);
        await staging.mount(plugin);
      }
    } catch (error) {
      await staging.dispose();
      throw error;
    }

    const previous = this.activeRuntime;
    this.target.contributions.replaceAll(staging.contributions);
    this.activeRuntime = staging;
    this.activeDigest = projection.clientGraphDigest;
    await previous?.dispose();
  }

  dispose(): Promise<void> {
    return this.enqueue(async () => {
      this.target.contributions.replaceAll(new StudioContributionRegistry());
      await this.activeRuntime?.dispose();
      this.activeRuntime = null;
      this.activeDigest = "";
    });
  }

  private enqueue(operation: () => Promise<void>): Promise<void> {
    const result = this.pending.then(operation);
    this.pending = result.catch(() => undefined);
    return result;
  }
}

export const studioDshCompositionHost = new StudioDshCompositionHost(studioDshRuntime);

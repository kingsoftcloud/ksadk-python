import { apiFetch } from "../api";
import {
  StudioContributionRegistry,
  STUDIO_DSH_SLOTS,
  type StudioSidebarNavigationContribution,
  type StudioRouteContribution,
  type StudioWorkspaceTabContribution,
} from "./studioContributions";
import type { DshUiExtensionPoint } from "./dshUiSandbox";
import { disposeDshUiSession, requestDshUiSession } from "./dshUiSandbox";
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
  url: string | null;
  sandboxCompatible?: boolean;
  sandboxBundleUrl?: string | null;
  executionMode?: string;
  sandboxIncompatibilityReason?: string | null;
}

export interface StudioDshProfileProjection {
  clientBundles: StudioDshClientBundleProjection[];
  clientGraphDigest: string;
}

/** A live UI session plus its declared extension points, for one plugin. */
interface SandboxSessionRecord {
  payload: Awaited<ReturnType<typeof requestDshUiSession>>;
}

/**
 * Register one sandbox session's extension points as declarative
 * contributions. The workspaceTab contribution carries the session payload so
 * the surface can mount the opaque-origin frame without a second lookup.
 */
function registerExtensionPoints(
  registry: StudioContributionRegistry,
  session: SandboxSessionRecord,
): Array<() => void> {
  const disposers: Array<() => void> = [];
  for (const point of session.payload.extensionPoints) {
    if (point.type === STUDIO_DSH_SLOTS.sidebarNavigation) {
      disposers.push(
        registry.register(STUDIO_DSH_SLOTS.sidebarNavigation, {
          id: point.id,
          label: point.label ?? point.id,
          path: point.path ?? "/extensions",
        } satisfies StudioSidebarNavigationContribution),
      );
    } else if (point.type === STUDIO_DSH_SLOTS.route) {
      disposers.push(
        registry.register(STUDIO_DSH_SLOTS.route, {
          id: point.id,
          path: point.path ?? "/extensions",
          title: point.label ?? point.id,
          workspaceTabId: point.workspaceTabId ?? "",
        } satisfies StudioRouteContribution),
      );
    } else if (point.type === STUDIO_DSH_SLOTS.workspaceTab) {
      disposers.push(
        registry.register(STUDIO_DSH_SLOTS.workspaceTab, {
          id: point.id,
          label: point.label ?? point.id,
          renderer: point.renderer,
          session: session.payload,
        } satisfies StudioWorkspaceTabContribution),
      );
    }
    // Unknown contribution types are ignored: the backend may add new slots
    // ahead of the frontend.
  }
  return disposers;
}

/**
 * Own the installed Profile's browser graph. Every enabled bundle must be
 * sandbox-compatible: it gets a UI session and declarative contributions.
 * There is no top-level script execution path (the legacy ModuleLoader
 * channel was retired; the backend default-denies it too). The graph becomes
 * visible only after it fully succeeds.
 */
export class StudioDshCompositionHost {
  private activeDigest = "";
  private activeRuntime: StudioDshRuntime | null = null;
  private activeSandboxDisposers: Array<() => void> = [];
  private activeSandboxSessionDisposers: Array<() => Promise<void>> = [];
  private pending: Promise<void> = Promise.resolve();

  constructor(private readonly target: StudioDshRuntime) {}

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
    const enabled = projection.clientBundles.filter(bundle => bundle.enabled);

    const staging = new StudioDshRuntime();
    const sandboxDisposers: Array<() => void> = [];
    const sandboxDisposeSessions: Array<() => Promise<void>> = [];
    try {
      for (const bundle of enabled) {
        if (!bundle.sandboxCompatible) {
          throw new Error(
            `DSH client bundle ${bundle.pluginId} is not sandbox-compatible: ${bundle.sandboxIncompatibilityReason || bundle.incompatibilityReason || "unknown reason"}`,
          );
        }
        const payload = await requestDshUiSession({
          pluginId: bundle.pluginId,
          clientDigest: bundle.digest,
        });
        const record: SandboxSessionRecord = { payload };
        sandboxDisposers.push(
          ...registerExtensionPoints(staging.contributions, record),
        );
        sandboxDisposeSessions.push(() => disposeDshUiSession(payload.uiSessionId));
      }
    } catch (error) {
      sandboxDisposers.forEach(dispose => dispose());
      await Promise.allSettled(sandboxDisposeSessions.map(dispose => dispose()));
      await staging.dispose();
      throw error;
    }

    const previous = this.activeRuntime;
    const previousSandboxDisposers = this.activeSandboxDisposers;
    const previousSandboxSessionDisposers = this.activeSandboxSessionDisposers;
    this.target.contributions.replaceAll(staging.contributions);
    this.activeRuntime = staging;
    this.activeSandboxDisposers = sandboxDisposers;
    this.activeSandboxSessionDisposers = sandboxDisposeSessions;
    this.activeDigest = projection.clientGraphDigest;
    previousSandboxDisposers.forEach(dispose => dispose());
    await Promise.allSettled(previousSandboxSessionDisposers.map(dispose => dispose()));
    await previous?.dispose();
  }

  dispose(): Promise<void> {
    return this.enqueue(async () => {
      this.target.contributions.replaceAll(new StudioContributionRegistry());
      this.activeSandboxDisposers.forEach(dispose => dispose());
      this.activeSandboxDisposers = [];
      await Promise.allSettled(
        this.activeSandboxSessionDisposers.map(dispose => dispose()),
      );
      this.activeSandboxSessionDisposers = [];
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

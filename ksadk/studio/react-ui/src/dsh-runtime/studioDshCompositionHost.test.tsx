import { afterEach, describe, expect, it, vi } from "vitest";
import { STUDIO_DSH_SLOTS } from "./studioContributions";
import {
  StudioDshCompositionHost,
  type StudioDshClientBundleProjection,
} from "./studioDshCompositionHost";
import { StudioDshRuntime } from "./studioDshRuntime";
import * as dshUiSandbox from "./dshUiSandbox";

const resources: Array<{ host: StudioDshCompositionHost; runtime: StudioDshRuntime }> = [];

function sandboxBundle(pluginId: string): StudioDshClientBundleProjection {
  return {
    compatible: false,
    contentBytes: 1,
    digest: `sha256:${"2".repeat(64)}`,
    enabled: true,
    external: [],
    inject: [],
    pluginId,
    url: null,
    sandboxCompatible: true,
    executionMode: "sandboxed-iframe",
  };
}

function incompatibleBundle(pluginId: string): StudioDshClientBundleProjection {
  return {
    ...sandboxBundle(pluginId),
    sandboxCompatible: false,
    sandboxIncompatibilityReason: "client bundle requires host module graph",
  };
}

function fakeSessionPayload(pluginId: string) {
  return {
    uiSessionId: `dshui_${pluginId}`,
    sourceId: `frame_${pluginId}`,
    expiresInSeconds: 900,
    protocolVersion: "agentkit.dsh-ui/v1",
    descriptorDigest: "sha256:dd",
    inventoryDigest: "sha256:ii",
    allowedTools: [],
    handshake: {
      protocolVersion: "agentkit.dsh-ui/v1",
      kind: "init" as const,
      sessionId: `dshui_${pluginId}`,
      sourceId: `frame_${pluginId}`,
      handshakeNonce: "nonce",
      capabilityToken: "token",
    },
    frame: {
      url: `/frame/${pluginId}`,
      sandbox: "allow-scripts",
      referrerPolicy: "no-referrer",
      credentialless: true,
    },
    extensionPoints: [
      {
        type: STUDIO_DSH_SLOTS.sidebarNavigation,
        id: `${pluginId}.navigation`,
        label: pluginId,
        path: `/extensions/${pluginId}`,
      },
      {
        type: STUDIO_DSH_SLOTS.route,
        id: `${pluginId}.route`,
        path: `/extensions/${pluginId}`,
        workspaceTabId: `${pluginId}.workspace`,
      },
      {
        type: STUDIO_DSH_SLOTS.workspaceTab,
        id: `${pluginId}.workspace`,
        label: pluginId,
        renderer: { type: "sandboxed-iframe" as const, frameUrl: `/frame/${pluginId}` },
      },
    ],
  };
}

afterEach(async () => {
  for (const resource of resources.splice(0)) {
    await resource.host.dispose();
    await resource.runtime.dispose();
  }
});

describe("Studio DSH production composition host", () => {
  it("atomically installs all declared slots and removes them when Profile inventory is empty", async () => {
    const runtime = new StudioDshRuntime();
    const sessionSpy = vi
      .spyOn(dshUiSandbox, "requestDshUiSession")
      .mockImplementation(async ({ pluginId }) => fakeSessionPayload(pluginId));
    vi.spyOn(dshUiSandbox, "disposeDshUiSession").mockResolvedValue(undefined);
    const host = new StudioDshCompositionHost(runtime);
    resources.push({ host, runtime });

    await host.reconcile({
      clientGraphDigest: "graph:a",
      clientBundles: [sandboxBundle("demo")],
    });
    expect(runtime.contributions.getEntries(STUDIO_DSH_SLOTS.sidebarNavigation)).toHaveLength(1);
    expect(runtime.contributions.getEntries(STUDIO_DSH_SLOTS.route)).toHaveLength(1);
    expect(runtime.contributions.getEntries(STUDIO_DSH_SLOTS.workspaceTab)).toHaveLength(1);

    await host.reconcile({ clientGraphDigest: "graph:empty", clientBundles: [] });
    for (const slot of Object.values(STUDIO_DSH_SLOTS)) {
      expect(runtime.contributions.getEntries(slot)).toHaveLength(0);
    }
    sessionSpy.mockRestore();
  });

  it("isolates a bundle that fails session creation as a degraded tab without aborting others", async () => {
    const runtime = new StudioDshRuntime();
    const sessionSpy = vi
      .spyOn(dshUiSandbox, "requestDshUiSession")
      .mockImplementation(async ({ pluginId }) => {
        if (pluginId === "broken") throw new Error("session creation failed");
        return fakeSessionPayload(pluginId);
      });
    vi.spyOn(dshUiSandbox, "disposeDshUiSession").mockResolvedValue(undefined);
    const host = new StudioDshCompositionHost(runtime);
    resources.push({ host, runtime });

    await host.reconcile({
      clientGraphDigest: "graph:stable",
      clientBundles: [sandboxBundle("stable")],
    });
    // A graph with a broken bundle alongside the stable one: the broken
    // bundle is isolated as a degraded tab, the stable one still activates.
    await host.reconcile({
      clientGraphDigest: "graph:broken",
      clientBundles: [sandboxBundle("stable"), sandboxBundle("broken")],
    });

    const tabs = runtime.contributions.getEntries(STUDIO_DSH_SLOTS.workspaceTab);
    const stable = tabs.find(t => t.id === "stable.workspace");
    const broken = tabs.find(t => t.id.startsWith("dsh.ui.failed."));
    expect(stable).toBeDefined();
    expect(stable?.session).toBeDefined();
    expect(broken).toBeDefined();
    expect(broken?.failureReason).toContain("session creation failed");
    sessionSpy.mockRestore();
  });

  it("isolates bundles that are not sandbox-compatible as degraded tabs without aborting the graph", async () => {
    const runtime = new StudioDshRuntime();
    const sessionSpy = vi.spyOn(dshUiSandbox, "requestDshUiSession");
    const host = new StudioDshCompositionHost(runtime);
    resources.push({ host, runtime });

    await host.reconcile({
      clientGraphDigest: "graph:legacy",
      clientBundles: [incompatibleBundle("legacy-plugin")],
    });

    // The incompatible bundle does not create a session.
    expect(sessionSpy).not.toHaveBeenCalled();
    // It registers a degraded tab with a failure reason instead of throwing.
    const tabs = runtime.contributions.getEntries(STUDIO_DSH_SLOTS.workspaceTab);
    expect(tabs).toHaveLength(1);
    expect(tabs[0].failureReason).toBeTruthy();
    sessionSpy.mockRestore();
  });

  it("registers declarative extension points for sandbox-compatible bundles without executing scripts", async () => {
    const runtime = new StudioDshRuntime();
    const sessionSpy = vi
      .spyOn(dshUiSandbox, "requestDshUiSession")
      .mockImplementation(async ({ pluginId }) => fakeSessionPayload(pluginId));
    const disposeSpy = vi
      .spyOn(dshUiSandbox, "disposeDshUiSession")
      .mockResolvedValue(undefined);
    const host = new StudioDshCompositionHost(runtime);
    resources.push({ host, runtime });

    await host.reconcile({
      clientGraphDigest: "graph:sandbox",
      clientBundles: [sandboxBundle("dsh-ui-plugin")],
    });

    expect(sessionSpy).toHaveBeenCalledWith({
      pluginId: "dsh-ui-plugin",
      clientDigest: `sha256:${"2".repeat(64)}`,
    });
    expect(runtime.contributions.getEntries(STUDIO_DSH_SLOTS.sidebarNavigation)).toHaveLength(1);
    expect(runtime.contributions.getEntries(STUDIO_DSH_SLOTS.route)).toHaveLength(1);
    const tabs = runtime.contributions.getEntries(STUDIO_DSH_SLOTS.workspaceTab);
    expect(tabs).toHaveLength(1);
    expect(tabs[0].renderer?.type).toBe("sandboxed-iframe");
    expect(tabs[0].session?.uiSessionId).toBe("dshui_dsh-ui-plugin");

    await host.reconcile({ clientGraphDigest: "graph:empty", clientBundles: [] });
    expect(runtime.contributions.getEntries(STUDIO_DSH_SLOTS.workspaceTab)).toHaveLength(0);
    expect(disposeSpy).toHaveBeenCalledWith("dshui_dsh-ui-plugin");

    sessionSpy.mockRestore();
    disposeSpy.mockRestore();
  });
});

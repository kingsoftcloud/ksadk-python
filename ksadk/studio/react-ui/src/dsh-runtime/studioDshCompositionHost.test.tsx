import { afterEach, describe, expect, it, vi } from "vitest";
import { STUDIO_DSH_SLOTS } from "./studioContributions";
import {
  StudioDshCompositionHost,
  type StudioDshClientBundleProjection,
} from "./studioDshCompositionHost";
import { StudioDshRuntime, type DshClientPlugin } from "./studioDshRuntime";
import * as dshUiSandbox from "./dshUiSandbox";

const resources: Array<{ host: StudioDshCompositionHost; runtime: StudioDshRuntime }> = [];

function bundle(pluginId: string): StudioDshClientBundleProjection {
  return {
    compatible: true,
    contentBytes: 1,
    digest: `sha256:${"1".repeat(64)}`,
    enabled: true,
    external: [],
    inject: [],
    pluginId,
    url: `/fixture/${pluginId}.js`,
  };
}

function completePlugin(id: string): DshClientPlugin {
  const Empty = () => null;
  return {
    name: id,
    apply(context) {
      context.studio.ui.register(context, STUDIO_DSH_SLOTS.sidebarNavigation, {
        id: `${id}.navigation`, label: id, path: `/extensions/${id}`,
      });
      context.studio.ui.register(context, STUDIO_DSH_SLOTS.route, {
        id: `${id}.route`, path: `/extensions/${id}`, title: id, workspaceTabId: `${id}.workspace`,
      });
      context.studio.ui.register(context, STUDIO_DSH_SLOTS.workspaceTab, {
        id: `${id}.workspace`, component: Empty, label: id,
      });
      context.studio.ui.register(context, STUDIO_DSH_SLOTS.settingsPage, {
        id: `${id}.settings`, component: Empty, label: id, path: `/settings/${id}`,
      });
      context.studio.ui.register(context, STUDIO_DSH_SLOTS.action, {
        id: `${id}.action`, label: id, path: `/extensions/${id}`,
      });
      context.studio.ui.register(context, STUDIO_DSH_SLOTS.renderer, {
        id: `${id}.renderer`, component: Empty, kind: `${id}.card`,
      });
    },
  };
}

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
  it("atomically installs all fixed slots and removes them when Profile inventory is empty", async () => {
    const runtime = new StudioDshRuntime();
    const host = new StudioDshCompositionHost(runtime, async item => completePlugin(item.pluginId));
    resources.push({ host, runtime });

    await host.reconcile({ clientGraphDigest: "graph:a", clientBundles: [bundle("demo")] });
    for (const slot of Object.values(STUDIO_DSH_SLOTS)) {
      if (slot === STUDIO_DSH_SLOTS.agentProvider) continue;
      expect(runtime.contributions.getEntries(slot)).toHaveLength(1);
    }

    await host.reconcile({ clientGraphDigest: "graph:empty", clientBundles: [] });
    for (const slot of Object.values(STUDIO_DSH_SLOTS)) {
      expect(runtime.contributions.getEntries(slot)).toHaveLength(0);
    }
  });

  it("keeps the old graph when a newly enabled bundle fails during Cordis activation", async () => {
    const runtime = new StudioDshRuntime();
    const host = new StudioDshCompositionHost(runtime, async item => item.pluginId === "broken"
      ? { apply(context) {
          context.studio.ui.register(context, STUDIO_DSH_SLOTS.action, { id: "partial", label: "partial" });
          throw new Error("activation failed");
        } }
      : completePlugin(item.pluginId));
    resources.push({ host, runtime });

    await host.reconcile({ clientGraphDigest: "graph:stable", clientBundles: [bundle("stable")] });
    await expect(host.reconcile({
      clientGraphDigest: "graph:broken",
      clientBundles: [bundle("broken")],
    })).rejects.toThrow("activation failed");

    expect(runtime.contributions.getEntries(STUDIO_DSH_SLOTS.action).map(item => item.id))
      .toEqual(["stable.action"]);
    expect(runtime.contributions.getEntries(STUDIO_DSH_SLOTS.sidebarNavigation).map(item => item.id))
      .toEqual(["stable.navigation"]);
  });

  it("registers declarative extension points for sandbox-compatible bundles without executing scripts", async () => {
    const runtime = new StudioDshRuntime();
    const sessionSpy = vi
      .spyOn(dshUiSandbox, "requestDshUiSession")
      .mockImplementation(async ({ pluginId }) => fakeSessionPayload(pluginId));
    const disposeSpy = vi
      .spyOn(dshUiSandbox, "disposeDshUiSession")
      .mockResolvedValue(undefined);
    // A bundle loader that throws if called proves sandbox bundles never touch
    // the top-level script execution path.
    const host = new StudioDshCompositionHost(runtime, async item => {
      throw new Error(`sandbox bundle ${item.pluginId} must not execute top-level`);
    });
    resources.push({ host, runtime });

    await host.reconcile({
      clientGraphDigest: "graph:sandbox",
      clientBundles: [sandboxBundle("dsh-ui-plugin")],
    });

    expect(sessionSpy).toHaveBeenCalledWith({
      pluginId: "dsh-ui-plugin",
      clientDigest: `sha256:${"2".repeat(64)}`,
    });
    expect(runtime.contributions.getEntries(STUDIO_DSH_SLOTS.sidebarNavigation))
      .toHaveLength(1);
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

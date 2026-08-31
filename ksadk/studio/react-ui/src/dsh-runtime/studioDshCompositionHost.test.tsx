import { afterEach, describe, expect, it } from "vitest";
import { STUDIO_DSH_SLOTS } from "./studioContributions";
import {
  StudioDshCompositionHost,
  type StudioDshClientBundleProjection,
} from "./studioDshCompositionHost";
import { StudioDshRuntime, type DshClientPlugin } from "./studioDshRuntime";

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
});

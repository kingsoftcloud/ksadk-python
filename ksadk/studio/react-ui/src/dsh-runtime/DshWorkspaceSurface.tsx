import {
  STUDIO_DSH_SLOTS,
  type StudioContributionRegistry,
  type StudioRouteContribution,
  type StudioWorkspaceTabContribution,
} from "./studioContributions";
import { studioDshRuntime } from "./studioDshRuntime";
import { DshUiSandboxFrame } from "./DshUiSandboxFrame";

function MissingPluginState({ label }: { label: string }) {
  return (
    <section className="empty-state" role="status">
      <h2>插件页面不可用</h2>
      <p>{label}</p>
    </section>
  );
}

/** A workspace tab backed by an opaque-origin sandbox iframe. */
function SandboxedWorkspaceTab({
  tab,
}: {
  tab: StudioWorkspaceTabContribution;
}) {
  const session = tab.session;
  if (!session) {
    return <MissingPluginState label="DSH UI session 未创建或已过期，请重新打开插件。" />;
  }
  return (
    <DshUiSandboxFrame
      session={session}
      title={tab.label}
    />
  );
}

export function DshWorkspaceSurface({
  currentAgentId,
  registry = studioDshRuntime.contributions,
  route,
}: {
  currentAgentId: string;
  registry?: StudioContributionRegistry;
  route: StudioRouteContribution;
}) {
  const tab = registry
    .getEntries(STUDIO_DSH_SLOTS.workspaceTab)
    .find(item => item.id === route.workspaceTabId);
  if (!tab) {
    return <MissingPluginState label="对应的 DSH workspace tab 已停用或卸载。" />;
  }

  // Sandbox renderer declared by the backend takes precedence.
  if (tab.renderer?.type === "sandboxed-iframe") {
    return <SandboxedWorkspaceTab tab={tab} />;
  }
  if (tab.component) {
    const Component = tab.component;
    return <Component active currentAgentId={currentAgentId} path={route.path} />;
  }
  return <MissingPluginState label="workspace tab 没有可用渲染器（component 或 renderer）。" />;
}

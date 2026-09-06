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
  onSessionExpired,
}: {
  tab: StudioWorkspaceTabContribution;
  onSessionExpired?: (sessionId: string) => void;
}) {
  const session = tab.session;
  if (!session) {
    return <MissingPluginState label="DSH UI session 未创建或已过期，请重新打开插件。" />;
  }
  return (
    <DshUiSandboxFrame
      session={session}
      title={tab.label}
      onSessionExpired={onSessionExpired}
    />
  );
}

export function DshWorkspaceSurface({
  currentAgentId,
  registry = studioDshRuntime.contributions,
  route,
  onSessionExpired,
}: {
  currentAgentId: string;
  registry?: StudioContributionRegistry;
  route: StudioRouteContribution;
  onSessionExpired?: (sessionId: string) => void;
}) {
  const tab = registry
    .getEntries(STUDIO_DSH_SLOTS.workspaceTab)
    .find(item => item.id === route.workspaceTabId);
  if (!tab) {
    return <MissingPluginState label="对应的 DSH workspace tab 已停用或卸载。" />;
  }

  // Sandbox renderer declared by the backend takes precedence.
  if (tab.renderer?.type === "sandboxed-iframe") {
    if (tab.failureReason) {
      return <MissingPluginState label={`插件 ${tab.label} 启动失败：${tab.failureReason}`} />;
    }
    return <SandboxedWorkspaceTab tab={tab} onSessionExpired={onSessionExpired} />;
  }
  if (tab.component) {
    const Component = tab.component;
    return <Component active currentAgentId={currentAgentId} path={route.path} />;
  }
  return <MissingPluginState label="workspace tab 没有可用渲染器（component 或 renderer）。" />;
}

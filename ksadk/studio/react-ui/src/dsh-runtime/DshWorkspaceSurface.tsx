import {
  STUDIO_DSH_SLOTS,
  type StudioContributionRegistry,
  type StudioRouteContribution,
} from "./studioContributions";
import { studioDshRuntime } from "./studioDshRuntime";

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
    return (
      <section className="empty-state" role="status">
        <h2>插件页面不可用</h2>
        <p>对应的 DSH workspace tab 已停用或卸载。</p>
      </section>
    );
  }
  const Component = tab.component;
  return <Component active currentAgentId={currentAgentId} path={route.path} />;
}

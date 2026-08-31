import { STUDIO_DSH_SLOTS, type StudioWorkspaceTabProps } from "../studioContributions";
import type { DshClientPlugin } from "../studioDshRuntime";

function TaskCanvas({ active, currentAgentId }: StudioWorkspaceTabProps) {
  return (
    <section data-testid="fixture-task-canvas" data-active={String(active)} data-current-agent={currentAgentId}>
      <h2>任务空间</h2>
      <p>{currentAgentId ? `已连接 Agent ${currentAgentId}` : "未选择 Agent"}</p>
    </section>
  );
}

/** A native DSH client plugin: one package can contribute UI and a provider. */
export const demoDshClientPlugin: DshClientPlugin = {
  name: "fixture-dsh-client-plugin",
  inject: ["studio"],
  apply(context) {
    context.studio.ui.register(context, STUDIO_DSH_SLOTS.sidebarNavigation, {
      id: "fixture.tasks.navigation",
      label: "任务空间",
      order: 35,
      path: "/extensions/tasks",
    });
    context.studio.ui.register(context, STUDIO_DSH_SLOTS.route, {
      id: "fixture.tasks.route",
      order: 35,
      path: "/extensions/tasks",
      title: "任务空间",
      workspaceTabId: "fixture.tasks.tab",
    });
    context.studio.ui.register(context, STUDIO_DSH_SLOTS.workspaceTab, {
      id: "fixture.tasks.tab",
      component: TaskCanvas,
      label: "任务空间",
      order: 35,
    });
    context.studio.ui.register(context, STUDIO_DSH_SLOTS.agentProvider, {
      id: "fixture.provider",
      compatible: true,
      displayName: "Fixture Runtime",
      order: 35,
      providerRef: "fixture.provider@1.0.0",
      state: "ready",
    });
  },
};

import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { afterEach, describe, expect, it } from "vitest";
import { NavigationRail } from "../components/NavigationRail";
import { DshWorkspaceSurface } from "./DshWorkspaceSurface";
import { demoDshClientPlugin } from "./fixtures/demoClientPlugin";
import { createStudioRuntimeOptions } from "./runtimeOptions";
import { STUDIO_DSH_SLOTS, useStudioDshContributions } from "./studioContributions";
import { StudioDshRuntime } from "./studioDshRuntime";

const runtimes: StudioDshRuntime[] = [];

function TestShell({ runtime }: { runtime: StudioDshRuntime }) {
  const navigation = useStudioDshContributions(runtime.contributions, STUDIO_DSH_SLOTS.sidebarNavigation);
  const routes = useStudioDshContributions(runtime.contributions, STUDIO_DSH_SLOTS.route);
  const providers = useStudioDshContributions(runtime.contributions, STUDIO_DSH_SLOTS.agentProvider);
  const [activePath, setActivePath] = useState("");
  const route = routes.find(item => item.path === activePath);
  const runtimeOptions = createStudioRuntimeOptions(providers);
  return (
    <>
      <NavigationRail
        view={route ? "extension" : "agents"}
        resourceKind="model"
        expanded
        workspaceName="fixture"
        workspacePath="/fixture"
        runtimeReady
        extensionItems={navigation}
        activeExtensionPath={activePath}
        onNavigate={() => undefined}
        onNavigateExtension={item => setActivePath(item.path)}
        onOpenSettings={() => undefined}
      />
      <output data-testid="runtime-options">{runtimeOptions.map(item => item.label).join("|")}</output>
      {route && <DshWorkspaceSurface currentAgentId="agent-42" registry={runtime.contributions} route={route} />}
    </>
  );
}

afterEach(async () => {
  await Promise.all(runtimes.splice(0).map(runtime => runtime.dispose()));
});

describe("Studio DSH/Cordis contribution host", () => {
  it("adds sidebar, workspace route and provider, then removes all contributions on dispose", async () => {
    const runtime = new StudioDshRuntime();
    runtimes.push(runtime);
    render(<TestShell runtime={runtime} />);

    expect(screen.queryByRole("button", { name: "任务空间" })).not.toBeInTheDocument();
    expect(screen.getByTestId("runtime-options")).not.toHaveTextContent("Fixture Runtime");

    let mounted: Awaited<ReturnType<StudioDshRuntime["mount"]>>;
    await act(async () => {
      mounted = await runtime.mount(demoDshClientPlugin);
    });

    expect(runtime.contributions.getEntries(STUDIO_DSH_SLOTS.sidebarNavigation)).toHaveLength(1);
    expect(runtime.contributions.getEntries(STUDIO_DSH_SLOTS.route)).toHaveLength(1);
    expect(runtime.contributions.getEntries(STUDIO_DSH_SLOTS.workspaceTab)).toHaveLength(1);
    expect(runtime.contributions.getEntries(STUDIO_DSH_SLOTS.agentProvider)).toHaveLength(1);

    const extensionButton = screen.getByRole("button", { name: "任务空间" });
    expect(screen.getByTestId("runtime-options")).toHaveTextContent("Fixture Runtime · Plugin");
    await userEvent.click(extensionButton);
    expect(screen.getByTestId("fixture-task-canvas")).toHaveAttribute("data-current-agent", "agent-42");
    expect(extensionButton).toHaveAttribute("aria-current", "page");

    await act(async () => {
      await mounted!.dispose();
    });

    expect(screen.queryByRole("button", { name: "任务空间" })).not.toBeInTheDocument();
    expect(screen.queryByTestId("fixture-task-canvas")).not.toBeInTheDocument();
    expect(screen.getByTestId("runtime-options")).not.toHaveTextContent("Fixture Runtime");
    expect(runtime.contributions.getEntries(STUDIO_DSH_SLOTS.sidebarNavigation)).toHaveLength(0);
    expect(runtime.contributions.getEntries(STUDIO_DSH_SLOTS.route)).toHaveLength(0);
    expect(runtime.contributions.getEntries(STUDIO_DSH_SLOTS.workspaceTab)).toHaveLength(0);
    expect(runtime.contributions.getEntries(STUDIO_DSH_SLOTS.agentProvider)).toHaveLength(0);
  });

  it("keeps a manifest-only or unhealthy provider out of Runtime selection", () => {
    expect(createStudioRuntimeOptions([{
      id: "not-ready",
      compatible: true,
      displayName: "Not Ready",
      providerRef: "not-ready@1.0.0",
      state: "degraded",
    }])).toEqual([
      { value: "codex", label: "Codex · ManagedRuntime" },
      { value: "adk", label: "Google ADK · Python source" },
      { value: "langgraph", label: "LangGraph · Python graph" },
    ]);
  });
});

import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  NavigationRail,
  readNavigationRailPreference,
  writeNavigationRailPreference,
} from "./NavigationRail";

const storedPreferences = new Map<string, string>();

describe("NavigationRail", () => {
  beforeEach(() => {
    storedPreferences.clear();
    Object.defineProperty(window, "localStorage", {
      configurable: true,
      value: {
        getItem: (key: string) => storedPreferences.get(key) ?? null,
        setItem: (key: string, value: string) => storedPreferences.set(key, value),
      },
    });
  });

  it("keeps compact state as the default and restores an explicit preference", () => {
    expect(readNavigationRailPreference()).toBeNull();
    writeNavigationRailPreference(true);
    expect(readNavigationRailPreference()).toBe(true);
    writeNavigationRailPreference(false);
    expect(readNavigationRailPreference()).toBe(false);
  });

  it("removes redundant product/user copy and exposes the full workspace path in a tooltip", async () => {
    const user = userEvent.setup();
    render(
      <NavigationRail
        view="agents"
        resourceKind="model"
        expanded
        workspaceName="studio-test"
        workspacePath="/Users/rain/projects/studio-test"
        runtimeReady
        onNavigate={() => undefined}
        onOpenSettings={() => undefined}
      />,
    );

    expect(screen.queryByText("Preview")).not.toBeInTheDocument();
    expect(screen.queryByText("Local User")).not.toBeInTheDocument();
    expect(screen.queryByText("/Users/rain/projects/studio-test")).not.toBeInTheDocument();
    await user.hover(screen.getByRole("button", { name: "studio-test 工作区" }));
    expect(await screen.findByRole("tooltip")).toHaveTextContent(
      "/Users/rain/projects/studio-test",
    );
  });

  it("routes every resource target without page-local navigation logic", async () => {
    const user = userEvent.setup();
    const onNavigate = vi.fn();
    const onOpenSettings = vi.fn();
    render(
      <NavigationRail
        view="resources"
        resourceKind="model"
        expanded={false}
        workspaceName="studio-test"
        workspacePath="/workspace/studio-test"
        runtimeReady
        onNavigate={onNavigate}
        onOpenSettings={onOpenSettings}
      />,
    );

    expect(screen.getByRole("complementary")).toHaveAttribute("data-state", "compact");
    await user.click(screen.getByRole("button", { name: "Skill" }));
    expect(onNavigate).toHaveBeenCalledWith("resources", "skill");
    await user.click(screen.getByRole("button", { name: "设置" }));
    expect(onOpenSettings).toHaveBeenCalledOnce();
  });
});

import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  NavigationRail,
  readNavigationRailPreference,
  writeNavigationRailPreference,
  type NavigationRailProps,
} from "./NavigationRail";
const storedPreferences = new Map<string, string>();
const props: NavigationRailProps = {
  view: "agents",
  resourceKind: "mcp",
  expanded: true,
  workspaceName: "studio-test",
  workspacePath: "/workspace/studio-test",
  runtimeReady: true,
  onNavigate: vi.fn(),
  onOpenSettings: vi.fn(),
};

describe("NavigationRail", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    storedPreferences.clear();
    Object.defineProperty(window, "localStorage", {
      configurable: true,
      value: {
        getItem: (key: string) => storedPreferences.get(key) ?? null,
        setItem: (key: string, value: string) =>
          storedPreferences.set(key, value),
      },
    });
  });
  it("renders only registered workspace contributions and removes disposed entries", async () => {
    const { rerender } = render(<NavigationRail {...props} workspacePages={[{id: "teams", label: "团队", pluginId: "test-teams"}]} />);
    await userEvent.click(screen.getByRole("button", {name: "团队"}));
    expect(props.onNavigate).toHaveBeenLastCalledWith("plugin:teams");
    rerender(<NavigationRail {...props} workspacePages={[]} />);
    expect(screen.queryByRole("button", {name: "团队"})).not.toBeInTheDocument();
  });
  it("persists an explicit rail preference", () => {
    expect(readNavigationRailPreference()).toBeNull();
    writeNavigationRailPreference(true);
    expect(readNavigationRailPreference()).toBe(true);
    writeNavigationRailPreference(false);
    expect(readNavigationRailPreference()).toBe(false);
  });
  it("keeps specialist routes behind four primary destinations and preserves every route", async () => {
    const user = userEvent.setup();
    render(<NavigationRail {...props} />);
    const nav = within(screen.getByRole("navigation", { name: "产品导航" }));
    expect(
      nav.getAllByRole("button").map((button) => button.textContent),
    ).toEqual(["新对话", "Agent", "资源库", "运行中心"]);
    await user.click(nav.getByRole("button", { name: "资源库" }));
    for (const [name, route] of [
      ["模型与工具", "resources"],
      ["运行资源", "runtime-resources"],
      ["插件", "plugins"],
    ]) {
      await user.click(nav.getByRole("button", { name }));
      expect(props.onNavigate).toHaveBeenLastCalledWith(
        route,
        route === "resources" ? "mcp" : undefined,
      );
    }
    await user.click(nav.getByRole("button", { name: "运行中心" }));
    expect(nav.queryByRole("button", { name: "插件" })).not.toBeInTheDocument();
    for (const [name, route] of [
      ["构建", "builds"],
      ["部署", "deployments"],
      ["自动化", "automations"],
      ["可观测", "observability"],
      ["评测", "evaluations"],
    ]) {
      await user.click(nav.getByRole("button", { name }));
      expect(props.onNavigate).toHaveBeenLastCalledWith(route, undefined);
    }
  });
  it("reveals the current nested destination after a deep link or route change", () => {
    const { rerender } = render(
      <NavigationRail {...props} view="evaluations" />,
    );
    expect(screen.getByRole("button", { name: "评测" })).toHaveAttribute(
      "aria-current",
      "page",
    );
    rerender(<NavigationRail {...props} view="runtime-resources" />);
    expect(
      screen.queryByRole("button", { name: "评测" }),
    ).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "运行资源" })).toHaveAttribute(
      "aria-current",
      "page",
    );
  });
  it("expands a compact rail when accessing grouped destinations", async () => {
    const onExpand = vi.fn();
    render(<NavigationRail {...props} expanded={false} onExpand={onExpand} />);
    await userEvent.click(screen.getByRole("button", { name: "运行中心" }));
    expect(onExpand).toHaveBeenCalledOnce();
  });
  it("shows workspace information without an inert fake button", async () => {
    render(<NavigationRail {...props} />);
    expect(
      screen.queryByRole("button", { name: "studio-test 工作区" }),
    ).not.toBeInTheDocument();
    await userEvent.hover(screen.getByLabelText("studio-test 工作区"));
    expect(await screen.findByRole("tooltip")).toHaveTextContent(
      "/workspace/studio-test",
    );
  });
  it("unmounts the closed mobile navigation and closes via Escape", async () => {
    const onMobileOpenChange = vi.fn();
    const { rerender } = render(
      <NavigationRail
        {...props}
        mobile
        mobileOpen={false}
        onMobileOpenChange={onMobileOpenChange}
      />,
    );
    expect(screen.queryByRole("navigation")).not.toBeInTheDocument();
    rerender(
      <NavigationRail
        {...props}
        mobile
        mobileOpen
        onMobileOpenChange={onMobileOpenChange}
      />,
    );
    expect(screen.getByRole("dialog", { name: "工作区导航" })).toBeVisible();
    await userEvent.keyboard("{Escape}");
    expect(onMobileOpenChange).toHaveBeenCalledWith(false);
  });
});

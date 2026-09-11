import { act, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import {
  useWorkspaceContributions,
  type WorkspaceContribution,
} from "./workspaceSlots";
import { PluginWorkspacePage } from "./PluginWorkspacePage";
vi.mock("../pages/TeamsAvailability", () => ({
  TeamsAvailability: () => <p>启用团队插件</p>,
}));
afterEach(() => {
  delete window.__STUDIO_DSH__;
});
it("discovers a late bridge and disposes its mounted contribution and subscription", () => {
  let pages: WorkspaceContribution[] = [];
  const dispose = vi.fn();
  const unsubscribe = vi.fn();
  function Host() {
    const contributions = useWorkspaceContributions();
    return (
      <>
        <nav>
          {contributions.map((page) => (
            <span key={page.id}>{page.label}</span>
          ))}
        </nav>
        <PluginWorkspacePage pageId="teams" contributions={contributions} />
      </>
    );
  }
  const { unmount } = render(<Host />);
  expect(screen.getByText("启用团队插件")).toBeInTheDocument();
  window.__STUDIO_DSH__ = {
    sections: () => [],
    subscribe: () => () => {},
    attach: () => () => {},
    workspacePages: () => pages,
    subscribeWorkspace: () => unsubscribe,
    attachWorkspace: vi.fn(() => dispose),
  };
  act(() => {
    pages = [{ id: "teams", label: "团队", pluginId: "test-plugin" }];
    window.dispatchEvent(new Event("studio:bridge-ready"));
  });
  expect(screen.getByText("团队")).toBeInTheDocument();
  expect(window.__STUDIO_DSH__.attachWorkspace).toHaveBeenCalledOnce();
  act(() => {
    pages = [];
    window.dispatchEvent(new Event("studio:workspace-changed"));
  });
  expect(dispose).toHaveBeenCalledOnce();
  expect(screen.queryByText("团队")).not.toBeInTheDocument();
  unmount();
  expect(unsubscribe).toHaveBeenCalledOnce();
});

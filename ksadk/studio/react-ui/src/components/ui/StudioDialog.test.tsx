import { useState } from "react";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { ConfirmDialog } from "../ConfirmDialog";
import { Drawer } from "../Drawer";
import { StudioDrawer } from "./StudioDialog";

function NestedModalHarness() {
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [confirmOpen, setConfirmOpen] = useState(false);
  return (
    <>
      <a className="skip-link" href="#mainContent">跳到内容</a>
      <aside className="sidebar">导航</aside>
      <header className="global-header">Agent</header>
      <main id="mainContent">
        <button type="button" onClick={() => setDrawerOpen(true)}>导入 Agent</button>
      </main>
      {drawerOpen ? (
        <Drawer title="导入 Agent" onClose={() => setDrawerOpen(false)}>
          <button type="button" onClick={() => setConfirmOpen(true)}>覆盖现有 Agent</button>
          {confirmOpen ? (
            <ConfirmDialog
              title="确认覆盖"
              onConfirm={() => undefined}
              onCancel={() => setConfirmOpen(false)}
            />
          ) : null}
        </Drawer>
      ) : null}
    </>
  );
}

describe("Studio modal primitives", () => {
  it("does not inert the application while a controlled drawer is closed", () => {
    render(
      <>
        <a className="skip-link" href="#mainContent">跳到内容</a>
        <aside className="sidebar">导航</aside>
        <header className="global-header">顶栏</header>
        <main id="mainContent">内容</main>
        <StudioDrawer open={false} onOpenChange={() => undefined} title="配置摘要">
          摘要内容
        </StudioDrawer>
      </>,
    );

    expect(document.querySelector(".skip-link")).not.toHaveAttribute("inert");
    expect(document.querySelector(".sidebar")).not.toHaveAttribute("inert");
    expect(document.querySelector(".global-header")).not.toHaveAttribute("inert");
    expect(document.querySelector("#mainContent")).not.toHaveAttribute("inert");
  });

  it("closes only the top layer and restores focus through nested dialogs", async () => {
    const user = userEvent.setup();
    render(<NestedModalHarness />);

    const importButton = screen.getByRole("button", { name: "导入 Agent" });
    await user.click(importButton);
    expect(document.querySelector(".skip-link")).toHaveAttribute("inert");
    expect(document.querySelector(".sidebar")).toHaveAttribute("inert");
    expect(document.querySelector(".global-header")).toHaveAttribute("inert");
    const overwriteButton = screen.getByRole("button", { name: "覆盖现有 Agent" });
    await user.click(overwriteButton);
    expect(screen.getByRole("alertdialog", { name: "确认覆盖" })).toBeVisible();

    await user.keyboard("{Escape}");
    expect(screen.queryByRole("alertdialog", { name: "确认覆盖" })).not.toBeInTheDocument();
    expect(screen.getByRole("dialog", { name: "导入 Agent" })).toBeVisible();
    await waitFor(() => expect(overwriteButton).toHaveFocus());

    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog", { name: "导入 Agent" })).not.toBeInTheDocument();
    await waitFor(() => expect(importButton).toHaveFocus());
    expect(document.querySelector(".skip-link")).not.toHaveAttribute("inert");
    expect(document.querySelector(".sidebar")).not.toHaveAttribute("inert");
    expect(document.querySelector(".global-header")).not.toHaveAttribute("inert");
  });
});

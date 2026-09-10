import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { MoreActionsMenu } from "../MoreActionsMenu";
import {
  StudioDataTable,
  type StudioDataColumn,
} from "./StudioDataTable";

interface Row {
  id: string;
  name: string;
}

const columns: StudioDataColumn<Row>[] = [
  { id: "name", header: "名称", cell: row => row.name },
];

describe("StudioDataTable", () => {
  it("marks the table viewport as the independently scrollable data region", () => {
    const { container } = render(
      <StudioDataTable
        columns={columns}
        data={[{ id: "1", name: "Model A" }]}
        getRowId={row => row.id}
      />,
    );
    expect(container.querySelector(".studio-data-table-scroll")).toHaveClass("data-scroll-region");
  });

  it("renders a shared loading state instead of stale rows", () => {
    render(
      <StudioDataTable
        columns={columns}
        data={[{ id: "1", name: "Old row" }]}
        getRowId={row => row.id}
        loading
      />,
    );
    expect(screen.getByRole("status")).toHaveTextContent("正在加载");
    expect(screen.queryByText("Old row")).not.toBeInTheDocument();
  });

  it("renders the shared empty state", () => {
    render(
      <StudioDataTable
        columns={columns}
        data={[]}
        getRowId={row => row.id}
        empty={{ title: "没有 Trace", description: "运行 Agent 后再查看。" }}
      />,
    );
    expect(screen.getByText("没有 Trace")).toBeVisible();
    expect(screen.getByText("运行 Agent 后再查看。")).toBeVisible();
  });

  it("opens an interactive row with Enter", async () => {
    const user = userEvent.setup();
    const onRowActivate = vi.fn();
    render(
      <StudioDataTable
        columns={columns}
        data={[{ id: "1", name: "Trace A" }]}
        getRowId={row => row.id}
        onRowActivate={onRowActivate}
      />,
    );
    const row = screen.getByRole("row", { name: /Trace A/ });
    row.focus();
    await user.keyboard("{Enter}");
    expect(onRowActivate).toHaveBeenCalledWith({ id: "1", name: "Trace A" });
  });

  it("exposes controlled cursor pagination without guessing the next page", async () => {
    const user = userEvent.setup();
    const onPreviousPage = vi.fn();
    const onNextPage = vi.fn();
    render(
      <StudioDataTable
        columns={columns}
        data={[{ id: "3", name: "Trace C" }, { id: "4", name: "Trace D" }]}
        getRowId={row => row.id}
        pagination={{
          pageIndex: 1,
          pageSize: 2,
          total: 5,
          hasNextPage: true,
          onPreviousPage,
          onNextPage,
        }}
      />,
    );
    expect(screen.getByText("第 2 页 · 3–4 / 5 条")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "上一页" }));
    await user.click(screen.getByRole("button", { name: "下一页" }));
    expect(onPreviousPage).toHaveBeenCalledOnce();
    expect(onNextPage).toHaveBeenCalledOnce();
  });

  it("offers a retry action for load errors", async () => {
    const user = userEvent.setup();
    const onRetry = vi.fn();
    render(
      <StudioDataTable
        columns={columns}
        data={[]}
        getRowId={row => row.id}
        error="Trace 加载失败"
        onRetry={onRetry}
      />,
    );
    expect(screen.getByRole("alert")).toHaveTextContent("Trace 加载失败");
    await user.click(screen.getByRole("button", { name: "重新加载" }));
    expect(onRetry).toHaveBeenCalledOnce();
  });
  it("hides empty headings and zero pagination, while later empty pages remain navigable", () => {
    const pagination = { pageIndex: 0, pageSize: 20, total: 0, hasNextPage: false, onPreviousPage: vi.fn(), onNextPage: vi.fn() };
    const { rerender } = render(<StudioDataTable<Row> columns={columns} data={[]} getRowId={row => row.id} pagination={pagination} />);
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /上一页/ })).not.toBeInTheDocument();
    rerender(<StudioDataTable<Row> columns={columns} data={[]} getRowId={row => row.id} pagination={{ ...pagination, pageIndex: 1 }} />);
    expect(screen.getByRole("button", { name: /上一页/ })).toBeEnabled();
  });

  it("does not open a row when a nested action is activated from the keyboard", async () => {
    const user = userEvent.setup();
    const onRowActivate = vi.fn();
    const onAction = vi.fn();
    render(<StudioDataTable<Row> columns={[{ id: "action", header: "操作", cell: () => <button onClick={onAction}>操作菜单</button> }]}
      data={[{ id: "1", name: "Agent" }]} getRowId={row => row.id} onRowActivate={onRowActivate} />);
    screen.getByRole("button", { name: "操作菜单" }).focus();
    await user.keyboard("{Enter}");
    expect(onAction).toHaveBeenCalledOnce();
    expect(onRowActivate).not.toHaveBeenCalled();
  });

  it.each(["pointer", "keyboard"])("keeps portaled menu actions from activating the row via %s", async method => {
    const user = userEvent.setup();
    const onRowActivate = vi.fn();
    const onDelete = vi.fn();
    render(<StudioDataTable<Row>
      columns={[{ id: "actions", header: "操作", cell: () => <MoreActionsMenu items={[{ label: "删除", onSelect: onDelete, danger: true }]} /> }]}
      data={[{ id: "1", name: "Agent" }]} getRowId={row => row.id} onRowActivate={onRowActivate} />);
    const trigger = screen.getByRole("button", { name: "更多操作" });
    if (method === "pointer") {
      await user.click(trigger);
      await user.click(screen.getByRole("menuitem", { name: "删除" }));
    } else {
      trigger.focus();
      await user.keyboard("{Enter}");
      screen.getByRole("menuitem", { name: "删除" }).focus();
      await user.keyboard("{Enter}");
    }
    expect(onDelete).toHaveBeenCalledOnce();
    expect(onRowActivate).not.toHaveBeenCalled();
  });
});

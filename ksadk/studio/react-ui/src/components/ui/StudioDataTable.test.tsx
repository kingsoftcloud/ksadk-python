import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
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
});

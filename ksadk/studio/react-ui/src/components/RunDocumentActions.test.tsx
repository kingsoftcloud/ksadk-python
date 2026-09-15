import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { apiFetch } from "../api";
import { documentLink, useRunDocumentActions } from "./RunDocumentActions";

vi.mock("../api", () => ({ apiFetch: vi.fn() }));
afterEach(() => vi.resetAllMocks());
const href = "/api/v1/runs/run-a/documents/content?path=report.md";
const meta = { name: "报告.md", path: "/workspace/report.md", relativePath: "report.md",
  capabilities: { open: true, reveal: true, openWith: true },
  applications: [{ id: "vscode", name: "VS Code" }, { id: "textedit", name: "文本编辑" }], revealLabel: "在 Finder 中显示" };
function Fixture() {
  const actions = useRunDocumentActions();
  return <div onClickCapture={actions.onClickCapture} onContextMenuCapture={actions.onContextMenuCapture}>
    <a href={href}>报告.md</a><a href="https://example.com">外部来源</a>{actions.ui}
  </div>;
}
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status });

describe("local generated file actions", () => {
  it("left click requests OS open, never renders a Studio preview", async () => {
    vi.mocked(apiFetch).mockResolvedValue(json({ status: "opened", name: "报告.md" }));
    render(<Fixture />);
    await userEvent.setup().click(screen.getByRole("link", { name: "报告.md" }));
    expect(apiFetch).toHaveBeenCalledWith("/api/v1/runs/run-a/documents/actions", expect.objectContaining({
      method: "POST", body: JSON.stringify({ path: "report.md", action: "open" }),
    }));
    expect(await screen.findByRole("status")).toHaveTextContent("系统默认应用");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("offers installed editors, reveal, copy and a real download on right click", async () => {
    vi.mocked(apiFetch).mockResolvedValue(json(meta));
    render(<Fixture />);
    fireEvent.contextMenu(screen.getByRole("link", { name: "报告.md" }), { clientX: 100, clientY: 60 });
    expect(await screen.findByRole("menuitem", { name: "在 VS Code 中打开" })).toBeVisible();
    expect(screen.getByRole("menuitem", { name: "复制路径" })).toBeVisible();
    expect(screen.getByRole("menuitem", { name: "下载副本…" })).toHaveAttribute("href", href);
    vi.mocked(apiFetch).mockResolvedValue(json({ status: "revealed", name: "报告.md" }));
    await userEvent.setup().click(screen.getByRole("menuitem", { name: "在 Finder 中显示" }));
    expect(apiFetch).toHaveBeenLastCalledWith("/api/v1/runs/run-a/documents/actions", expect.objectContaining({ body: JSON.stringify({ path: "report.md", action: "reveal" }) }));
  });

  it("copies file content without opening it or exposing it in the page", async () => {
    const user = userEvent.setup();
    const clipboard = vi.spyOn(navigator.clipboard, "writeText").mockResolvedValue();
    vi.mocked(apiFetch).mockResolvedValueOnce(json(meta)).mockResolvedValueOnce(json({ content: "# 文件内容" }));
    render(<Fixture />);
    fireEvent.contextMenu(screen.getByRole("link", { name: "报告.md" }));
    await user.click(await screen.findByRole("menuitem", { name: "复制文件内容" }));
    await waitFor(() => expect(clipboard).toHaveBeenCalledWith("# 文件内容"));
    expect(screen.queryByText("# 文件内容")).not.toBeInTheDocument();
  });

  it.each(["click", "hover"] as const)("selects an application from the Open With submenu after %s", async interaction => {
    const user = userEvent.setup();
    vi.mocked(apiFetch).mockResolvedValueOnce(json(meta)).mockResolvedValueOnce(json({ name: "报告.md", status: "opened" }));
    render(<Fixture />);
    fireEvent.contextMenu(screen.getByRole("link", { name: "报告.md" }));
    await user[interaction](await screen.findByRole("menuitem", { name: "打开方式" }));
    await user.click(await screen.findByRole("menuitem", { name: "文本编辑" }));
    await waitFor(() => expect(apiFetch).toHaveBeenLastCalledWith("/api/v1/runs/run-a/documents/actions", expect.objectContaining({ body: JSON.stringify({ path: "report.md", action: "open", applicationId: "textedit" }) })));
  });

  it("selects an Open With application with keyboard navigation", async () => {
    const user = userEvent.setup();
    vi.mocked(apiFetch).mockResolvedValueOnce(json(meta)).mockResolvedValueOnce(json({ name: "报告.md", status: "opened" }));
    render(<Fixture />);
    fireEvent.contextMenu(screen.getByRole("link", { name: "报告.md" }));
    await user.click(await screen.findByRole("menuitem", { name: "打开方式" }));
    await user.keyboard("{ArrowRight}{End}{Enter}");
    await waitFor(() => expect(apiFetch).toHaveBeenLastCalledWith("/api/v1/runs/run-a/documents/actions", expect.objectContaining({ body: JSON.stringify({ path: "report.md", action: "open", applicationId: "textedit" }) })));
    expect(screen.queryByRole("menu")).not.toBeInTheDocument();
  });

  it("keeps normal submenu back-navigation, Escape and focus restoration", async () => {
    const user = userEvent.setup();
    vi.mocked(apiFetch).mockResolvedValue(json(meta));
    render(<Fixture />);
    const link = screen.getByRole("link", { name: "报告.md" });
    fireEvent.contextMenu(link);
    const submenu = await screen.findByRole("menuitem", { name: "打开方式" });
    await user.click(submenu);
    await user.keyboard("{ArrowRight}{ArrowLeft}");
    expect(submenu).toHaveAttribute("aria-expanded", "false");
    expect(submenu).toHaveFocus();
    expect(screen.getByRole("menu")).toBeVisible();
    await user.keyboard("{ArrowRight}{Escape}");
    await waitFor(() => expect(screen.queryByRole("menu")).not.toBeInTheDocument());
    await waitFor(() => expect(link).toHaveFocus());
    expect(apiFetch).toHaveBeenCalledTimes(1);
  });

  it("closes the submenu when the pointer moves to another parent item", async () => {
    const user = userEvent.setup();
    vi.mocked(apiFetch).mockResolvedValue(json(meta));
    render(<Fixture />);
    fireEvent.contextMenu(screen.getByRole("link", { name: "报告.md" }));
    const submenu = await screen.findByRole("menuitem", { name: "打开方式" });
    await user.click(submenu);
    expect(await screen.findByRole("menuitem", { name: "文本编辑" })).toBeVisible();
    await user.hover(screen.getByRole("menuitem", { name: "复制路径" }));
    await waitFor(() => expect(submenu).toHaveAttribute("aria-expanded", "false"));
    expect(screen.queryByRole("menuitem", { name: "文本编辑" })).not.toBeInTheDocument();
    expect(apiFetch).toHaveBeenCalledTimes(1);
  });

  it("does not pretend remote or missing files can be opened", async () => {
    vi.mocked(apiFetch).mockResolvedValue(json({ ...meta, path: null, capabilities: { open: false, reveal: false, openWith: false }, applications: [] }));
    render(<Fixture />);
    fireEvent.contextMenu(screen.getByRole("link", { name: "报告.md" }));
    expect(await screen.findByRole("menuitem", { name: "打开文件" })).toHaveAttribute("data-disabled");
    expect(screen.queryByRole("menuitem", { name: "在 VS Code 中打开" })).toBeNull();
    await userEvent.setup().keyboard("{Escape}");
    vi.mocked(apiFetch).mockResolvedValue(json({}, 404));
    await userEvent.setup().click(screen.getByRole("link", { name: "报告.md" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("移动或删除");
  });

  it("only intercepts exact same-origin document service links", () => {
    for (const link of ["https://example.com/api/v1/runs/a/documents/content?path=x", "javascript:alert(1)", "/other?path=x", "/api/v1/runs/a/documents/content"]) {
      const anchor = document.createElement("a"); anchor.href = link;
      expect(documentLink(anchor)).toBeNull();
    }
    const anchor = document.createElement("a"); anchor.href = href; anchor.setAttribute("data-run-document-download", "");
    expect(documentLink(anchor)).toBeNull();
  });
});

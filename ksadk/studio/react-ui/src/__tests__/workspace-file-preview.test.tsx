import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { FilePreviewHost, openWorkspaceFilePreview } from "@kingsoftcloud/ksadk-web/file-preview";

vi.mock("../api", () => ({ apiFetch: vi.fn() }));

const apiFetchMock = vi.mocked((await import("../api")).apiFetch);
const response = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status });

beforeEach(() => {
  apiFetchMock.mockReset();
});

describe("workspace file preview integration", () => {
  it("previews a markdown file created by the agent from its path in chat", async () => {
    const filePath = "/Users/xiayu/agentengine-test/studio-test/AI芯片最新消息_2026年9月.md";
    apiFetchMock.mockResolvedValue(
      response({
        path: filePath,
        name: "AI芯片最新消息_2026年9月.md",
        sizeBytes: 8045,
        contentType: "text/markdown",
        mediaCategory: "markdown",
        content: "# 结论摘要（四大方向）\n\n国产 AI 算力重大突破",
      }),
    );
    render(
      <>
        <FilePreviewHost fetchFile={(path) => apiFetchMock(path)} />
        <button type="button" onClick={() => openWorkspaceFilePreview(filePath)}>打开文件预览</button>
      </>,
    );
    fireEvent.click(screen.getByRole("button", { name: "打开文件预览" }));
    // fetcher 收到原始路径；URL 编码由默认 fetcher 负责
    expect(apiFetchMock.mock.calls[0]?.[0]).toBe(filePath);
    expect(await screen.findByRole("dialog")).toBeTruthy();
    await waitFor(() => expect(screen.getByText("国产 AI 算力重大突破")).toBeTruthy());
    fireEvent.click(screen.getByRole("button", { name: "关闭预览" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  });

  it("surfaces forbidden-path errors from the backend guard", async () => {
    apiFetchMock.mockResolvedValue(
      response({ error: { code: "WORKSPACE_FILE_FORBIDDEN", message: "路径不在工作区（或链接目录）内" } }, 403),
    );
    render(<FilePreviewHost fetchFile={(path) => apiFetchMock(path)} />);
    const { openWorkspaceFilePreview: open } = await import("@kingsoftcloud/ksadk-web/file-preview");
    open("/etc/passwd");
    expect(await screen.findByRole("alert")).toHaveTextContent("路径不在工作区");
  });
});

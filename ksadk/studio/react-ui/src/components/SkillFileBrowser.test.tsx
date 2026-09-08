import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { apiFetch } from "../api";
import { SkillFileBrowser } from "./SkillFileBrowser";

vi.mock("../api", () => ({ apiFetch: vi.fn() }));
vi.mock("./Drawer", () => ({
  Drawer: ({ title, subtitle, children }: any) => (
    <section aria-label={title}><p>{subtitle}</p>{children}</section>
  ),
  InlineAlert: ({ title, message }: any) => <div role="alert">{title}: {message}</div>,
}));

const mockedApiFetch = vi.mocked(apiFetch);

function jsonResponse(payload: unknown): Response {
  return new Response(JSON.stringify(payload), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

describe("SkillFileBrowser", () => {
  beforeEach(() => {
    mockedApiFetch.mockReset();
    mockedApiFetch.mockImplementation(async input => {
      const url = String(input);
      if (!url.includes("?")) {
        return jsonResponse({
          files: [
            { path: "SKILL.md", size: 48, kind: "markdown" },
            { path: "scripts/run.py", size: 24, kind: "script" },
          ],
        });
      }
      const path = new URL(url, window.location.href).searchParams.get("path");
      if (path === "scripts/run.py") {
        return jsonResponse({
          path,
          size: 24,
          kind: "script",
          content: "def run():\n    return True",
        });
      }
      return jsonResponse({
        path: "SKILL.md",
        size: 48,
        kind: "markdown",
        content: "# Review Skill\n\nRead the diff.",
      });
    });
  });

  it("shares one read-only browser for Markdown and script previews", async () => {
    const user = userEvent.setup();
    render(
      <SkillFileBrowser
        title="review-skill"
        endpoint="/api/v1/skills/review/files"
        onClose={() => undefined}
      />,
    );

    expect(await screen.findByRole("heading", { name: "Review Skill" })).toBeVisible();
    expect(screen.getByText("2 个文件；只读预览，不会执行脚本。")).toBeVisible();

    await user.click(screen.getByRole("treeitem", { name: /run\.py/ }));
    expect(await screen.findByRole("region", { name: "scripts/run.py 源码" })).toBeVisible();
    await waitFor(() => expect(mockedApiFetch).toHaveBeenCalledWith(
      "/api/v1/skills/review/files?path=scripts%2Frun.py",
      expect.objectContaining({ signal: expect.any(AbortSignal) }),
    ));
  });
});

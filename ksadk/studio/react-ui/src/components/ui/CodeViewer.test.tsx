import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it } from "vitest";
import { CodeViewer } from "./CodeViewer";
import { MarkdownPreview } from "./MarkdownPreview";

afterEach(() => {
  document.documentElement.classList.remove("dark");
});

describe("CodeViewer", () => {
  it("renders semantic Prism tokens in both Studio themes", async () => {
    const { rerender } = render(
      <CodeViewer
        code={'const answer: string = "ready";'}
        language="typescript"
        filename="status.ts"
        showLineNumbers
      />,
    );

    const lightKeyword = await screen.findByText("const");
    expect(lightKeyword).toHaveClass("token", "keyword");
    expect(lightKeyword).toHaveStyle({ color: "var(--code-token-keyword)" });
    expect(screen.getByText('"ready"')).toHaveStyle({ color: "var(--code-token-string)" });

    document.documentElement.classList.add("dark");
    rerender(
      <CodeViewer
        code={'def greet(name: str):\n    return f"hello {name}"'}
        language="python"
        filename="tool.py"
        showLineNumbers
      />,
    );

    const darkKeyword = await screen.findByText("def");
    expect(darkKeyword).toHaveStyle({ color: "var(--code-token-keyword)" });
    expect(screen.getByRole("region", { name: "tool.py 源码" })).toHaveAttribute(
      "data-code-theme",
      "studio",
    );
  });

  it("offers an accessible wrap control", async () => {
    const user = userEvent.setup();
    render(<CodeViewer code="const value = 1;" language="typescript" filename="a.ts" />);

    const viewport = await screen.findByRole("region", { name: "a.ts 源码" });
    expect(viewport).toHaveAttribute("data-wrap", "false");
    await user.click(screen.getByRole("button", { name: "自动换行" }));
    await waitFor(() => expect(viewport).toHaveAttribute("data-wrap", "true"));
  });
});

describe("MarkdownPreview", () => {
  it("collapses frontmatter and renders GFM tables and fenced code", async () => {
    render(
      <MarkdownPreview
        content={`---\nname: review-skill\nversion: 1.0.0\n---\n# Review Skill\n\n| Input | Output |\n| --- | --- |\n| diff | findings |\n\n\`\`\`typescript\nconst finding = "P1";\n\`\`\``}
      />,
    );

    const metadata = screen.getByText("文档元数据").closest("details");
    expect(metadata).not.toHaveAttribute("open");
    expect(screen.getByRole("table")).toBeVisible();
    expect(screen.getByRole("heading", { name: "Review Skill" })).toBeVisible();
    expect(await screen.findByRole("region", { name: "typescript 代码" })).toBeVisible();
  });
});

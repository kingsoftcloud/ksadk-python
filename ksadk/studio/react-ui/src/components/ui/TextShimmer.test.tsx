import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { TextShimmer } from "./TextShimmer";

describe("TextShimmer", () => {
  it("renders the first-phase text for early stages", () => {
    render(<TextShimmer stage="resolving_model" />);
    const el = screen.getByTestId("authoring-stage-shimmer");
    expect(el.querySelector(".text-shimmer")?.textContent).toBe("正在理解你的需求…");
    expect(el.textContent).toContain("分析对话内容，确定 Agent 的框架与能力");
    expect(el).toHaveAttribute("data-stage", "resolving_model");
  });

  it("switches to the second-phase text once the backend validates", () => {
    render(<TextShimmer stage="validating" />);
    expect(screen.getByTestId("authoring-stage-shimmer").querySelector(".text-shimmer")?.textContent).toBe("正在校验配置…");
  });

  it("falls back to the first-phase text without a stage", () => {
    render(<TextShimmer stage={null} />);
    const el = screen.getByTestId("authoring-stage-shimmer");
    expect(el.querySelector(".text-shimmer")?.textContent).toBe("正在理解你的需求…");
    expect(el).toHaveAttribute("data-stage", "unknown");
  });
});

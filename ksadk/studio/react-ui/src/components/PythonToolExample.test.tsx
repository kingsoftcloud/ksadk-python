import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { PythonToolExample } from "./PythonToolExample";

describe("PythonToolExample", () => {
  it("reveals and collapses a copyable Python authoring example", async () => {
    const user = userEvent.setup();
    render(<PythonToolExample />);

    const trigger = screen.getByRole("button", { name: "查看编写示例" });
    expect(trigger).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByRole("region", { name: "tool_example.py 源码" })).not.toBeInTheDocument();

    await user.click(trigger);

    expect(screen.getByRole("button", { name: "收起编写示例" })).toHaveAttribute(
      "aria-expanded",
      "true",
    );
    expect(await screen.findByRole("region", { name: "tool_example.py 源码" })).toBeVisible();
    expect(screen.getByRole("button", { name: "复制代码" })).toBeVisible();

    await user.click(screen.getByRole("button", { name: "收起编写示例" }));

    expect(screen.queryByRole("region", { name: "tool_example.py 源码" })).not.toBeInTheDocument();
  });
});

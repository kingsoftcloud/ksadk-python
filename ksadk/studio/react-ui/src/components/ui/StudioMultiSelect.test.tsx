import { useState } from "react";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { StudioMultiSelect } from "./StudioMultiSelect";

const models = [
  { id: "glm-5.2", label: "GLM-5.2", description: "凭证已配置" },
  { id: "kimi-k2", label: "Kimi K2", description: "需要凭证" },
];

function Harness() {
  const [selected, setSelected] = useState<string[]>([]);
  return (
    <StudioMultiSelect
      ariaLabel="选择模型"
      items={models}
      selectedIds={selected}
      getId={item => item.id}
      getLabel={item => item.label}
      getDescription={item => item.description}
      onChange={setSelected}
      searchPlaceholder="搜索模型"
      emptyMessage="没有模型"
    />
  );
}

describe("StudioMultiSelect", () => {
  it("supports keyboard selection and keeps selected chips visible", async () => {
    const user = userEvent.setup();
    render(<Harness />);

    await user.click(screen.getByRole("button", { name: "选择模型" }));
    await user.type(screen.getByRole("combobox", { name: "搜索模型" }), "glm");
    await user.keyboard("{ArrowDown}{Enter}{Escape}");

    const selection = screen.getByTestId("studio-multi-select-selection");
    expect(within(selection).getByText("GLM-5.2")).toBeVisible();
    expect(screen.getByText("已选 1 个")).toBeVisible();
  });

  it("can clear selections without reopening the scrolling list", async () => {
    const user = userEvent.setup();
    render(<Harness />);
    await user.click(screen.getByRole("button", { name: "选择模型" }));
    await user.click(screen.getByRole("option", { name: /GLM-5.2/ }));
    await user.keyboard("{Escape}");
    await user.click(screen.getByRole("button", { name: "移除 GLM-5.2" }));
    expect(screen.getByText("尚未选择")).toBeVisible();
  });
});

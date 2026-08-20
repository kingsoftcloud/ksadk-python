import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { FormField } from "./FormField";

describe("FormField", () => {
  it("shows requirement metadata, hint and an accessible error", () => {
    render(
      <FormField
        htmlFor="agent-name"
        label="名称"
        requirement="required"
        hint="用于工作区列表。"
        error="必须填写"
      >
        <input id="agent-name" />
      </FormField>,
    );

    expect(screen.getByText("*")).toBeVisible();
    expect(screen.getByLabelText("名称说明")).toBeVisible();
    expect(screen.getByRole("textbox", { name: /名称/ })).toHaveAccessibleDescription("用于工作区列表。 必须填写");
    expect(screen.getByRole("alert")).toHaveTextContent("必须填写");
    expect(screen.getByText("名称").closest("label")).toHaveAttribute("for", "agent-name");
  });

  it("omits redundant optional copy", () => {
    render(<FormField label="描述" requirement="optional"><input /></FormField>);
    expect(screen.queryByText("选填")).not.toBeInTheDocument();
  });

  it("renders generated metadata", () => {
    render(<FormField label="本地标识" requirement="generated"><input /></FormField>);
    expect(screen.getByText("自动生成")).toBeVisible();
  });
});

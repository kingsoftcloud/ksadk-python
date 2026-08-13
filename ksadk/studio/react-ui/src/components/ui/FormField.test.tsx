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

    expect(screen.getByText("必填")).toBeVisible();
    expect(screen.getByText("用于工作区列表。")).toBeVisible();
    expect(screen.getByRole("alert")).toHaveTextContent("必须填写");
    expect(screen.getByText("名称").closest("label")).toHaveAttribute("for", "agent-name");
  });

  it.each([
    ["optional", "选填"],
    ["generated", "自动生成"],
  ] as const)("renders the %s requirement", (requirement, copy) => {
    render(<FormField label="本地标识" requirement={requirement}><input /></FormField>);
    expect(screen.getByText(copy)).toBeVisible();
  });
});

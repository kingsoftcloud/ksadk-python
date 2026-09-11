import { useState } from "react";
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { SubAgentBindingsEditor, validateSubAgentBindings, type SubAgentBinding } from "./SubAgentBindingsEditor";

describe("SubAgent bindings", () => {
  it("edits a real declaration while retaining its advanced limits and restricting tools to the parent catalog", () => {
    let current: SubAgentBinding[] = [];
    function Wrapper() {
      const [rows, setRows] = useState<SubAgentBinding[]>([{ name: "reviewer", instructions: "Review the work", maxTotalTokens: 3000, failurePolicy: "return_error", tools: [] }]);
      current = rows;
      return <SubAgentBindingsEditor value={rows} tools={[{ name: "read_file", label: "读取文件" }]} onChange={setRows} />;
    }
    render(<Wrapper />);
    fireEvent.click(screen.getByText("子 Agent", { exact: false, selector: "summary" }));
    fireEvent.change(screen.getByLabelText("用途说明"), { target: { value: "Independent review" } });
    fireEvent.click(screen.getByLabelText("读取文件"));
    expect(current[0]).toMatchObject({ description: "Independent review", tools: ["read_file"], maxTotalTokens: 3000, failurePolicy: "return_error" });
    fireEvent.click(screen.getByRole("button", { name: "添加子 Agent" }));
    expect(current[1]).toMatchObject({ name: "helper_2", tools: [], inheritSkills: false, inheritMcp: false });
    expect(validateSubAgentBindings(current, "harness")).toContain("提示词");
  });
  it("prevents runtime switches or duplicate declarations from passing local validation", () => {
    const row = { name: "reviewer", instructions: "Review the result" };
    expect(validateSubAgentBindings([row], "codex")).toContain("Harness");
    expect(validateSubAgentBindings([row, row], "harness")).toContain("重复");
    expect(validateSubAgentBindings([{ ...row, maxTurns: 33 }], "harness")).toContain("1–32");
    expect(validateSubAgentBindings([row], "harness")).toBeNull();
  });
});

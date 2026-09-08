import { useState } from "react";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { GeneratedIdField } from "./GeneratedIdField";

function Harness() {
  const [value, setValue] = useState("agentkit-0011aaff");
  return <GeneratedIdField value={value} onChange={setValue} generate={() => "agentkit-a1b2c3d4"} />;
}

describe("GeneratedIdField", () => {
  it("preserves manual edits and supports explicit regeneration", async () => {
    const user = userEvent.setup();
    render(<Harness />);
    const input = screen.getByRole("textbox", { name: /本地标识/ });

    await user.clear(input);
    await user.type(input, "my-agent");
    expect(input).toHaveValue("my-agent");

    await user.click(screen.getByRole("button", { name: "重新生成本地标识" }));
    expect(input).toHaveValue("agentkit-a1b2c3d4");
  });
});

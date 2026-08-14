import { describe, expect, it } from "vitest";
import { cn } from "./utils";

describe("cn", () => {
  it("joins conditional classes and resolves conflicting Tailwind utilities", () => {
    expect(cn("px-2", false, "px-4", { "text-sm": true })).toBe("px-4 text-sm");
  });
});

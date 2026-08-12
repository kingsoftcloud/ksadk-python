import { describe, expect, it } from "vitest";
import { cn } from "./utils";

describe("cn", () => {
  it("merges conditional class names", () => {
    expect(cn("button", false && "hidden", "accent")).toBe("button accent");
  });
});

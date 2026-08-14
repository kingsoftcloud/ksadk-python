import { describe, expect, it } from "vitest";
import { generateAgentSlug } from "./generatedId";

describe("generateAgentSlug", () => {
  it("returns an opaque local identifier", () => {
    expect(generateAgentSlug(() => new Uint8Array([0, 17, 170, 255])))
      .toBe("agentkit-0011aaff");
  });
});

import { describe, expect, it } from "vitest";
import { generateAgentSlug } from "./generatedId";

describe("generateAgentSlug", () => {
  it("generates a local id accepted by the agent form", () => {
    expect(generateAgentSlug()).toMatch(/^[a-z][a-z0-9-]{2,62}$/);
  });
});

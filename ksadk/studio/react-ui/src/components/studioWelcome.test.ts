import { describe, expect, it } from "vitest";
import { pickStudioWelcome, STUDIO_WELCOME_COPY } from "./studioWelcome";

describe("Studio welcome copy", () => {
  it("selects a bounded message from the fixed copy pool", () => {
    expect(pickStudioWelcome(() => 0)).toBe(STUDIO_WELCOME_COPY[0]);
    expect(pickStudioWelcome(() => 0.99999)).toBe(STUDIO_WELCOME_COPY.at(-1));
  });
});

import { describe, expect, it } from "vitest";
import { normalizeSandbox } from "./SettingsOverlay";

describe("normalizeSandbox", () => {
  it("maps persisted legacy values to the options rendered by Studio", () => {
    expect(normalizeSandbox("read_only")).toBe("read-only");
    expect(normalizeSandbox("workspace_write")).toBe("workspace-write");
    expect(normalizeSandbox("workspace_write_auto")).toBe("workspace-write-auto");
    expect(normalizeSandbox("full_access")).toBe("full-access");
  });

  it("falls back to the least privileged option for missing or invalid values", () => {
    expect(normalizeSandbox()).toBe("read-only");
    expect(normalizeSandbox("unexpected")).toBe("read-only");
  });
});

import { describe, expect, it } from "vitest";
import { normalizeHarnessSandbox, normalizeSandbox } from "./SettingsPage";

describe("normalizeSandbox", () => {
  it("maps persisted legacy values to the options rendered by Studio", () => {
    expect(normalizeSandbox("read_only")).toBe("read-only");
    expect(normalizeSandbox("workspace_write")).toBe("workspace-write");
    expect(normalizeSandbox("workspace_write_auto")).toBe("workspace-write-auto");
    expect(normalizeSandbox("full_access")).toBe("full-access");
  });

  it("falls back to the least privileged option for missing or invalid values", () => {
    expect(normalizeSandbox()).toBe("workspace-write-auto");
    expect(normalizeSandbox("unexpected")).toBe("workspace-write-auto");
  });
});

describe("normalizeHarnessSandbox (G7)", () => {
  it("maps persisted underscore values to the harness isolation-strength enum", () => {
    expect(normalizeHarnessSandbox("read_only")).toBe("read-only");
    expect(normalizeHarnessSandbox("workspace_write_local")).toBe("workspace-write-local");
    expect(normalizeHarnessSandbox("workspace_write_remote")).toBe("workspace-write-remote");
    expect(normalizeHarnessSandbox("full_access")).toBe("full-access");
  });

  it("falls back to workspace-write-local for missing or invalid values", () => {
    expect(normalizeHarnessSandbox()).toBe("workspace-write-local");
    expect(normalizeHarnessSandbox("unexpected")).toBe("workspace-write-local");
    // 旧 codex 值不混入 harness 档位
    expect(normalizeHarnessSandbox("workspace-write-auto")).toBe("workspace-write-local");
  });
});

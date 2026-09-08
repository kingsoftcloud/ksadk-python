import { describe, expect, it, vi } from "vitest";
import { applyApiFieldErrors } from "./formErrors";

describe("applyApiFieldErrors", () => {
  it("maps a Studio field error into the form setter", () => {
    const setError = vi.fn();
    expect(applyApiFieldErrors({ error: { field: "spec.model", message: "模型无效" } }, setError)).toBe(true);
    expect(setError).toHaveBeenCalledWith("spec.model", { type: "server", message: "模型无效" });
  });

  it("returns false for a non-field error", () => {
    const setError = vi.fn();
    expect(applyApiFieldErrors({ error: { message: "请求失败" } }, setError)).toBe(false);
    expect(setError).not.toHaveBeenCalled();
  });
});

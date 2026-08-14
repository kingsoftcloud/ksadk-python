import { describe, expect, it, vi } from "vitest";
import { applyApiFieldErrors } from "./formErrors";

describe("applyApiFieldErrors", () => {
  it("maps direct and grouped API field errors", () => {
    const setError = vi.fn();
    expect(applyApiFieldErrors({
      error: {
        field: "slug",
        message: "已存在",
        details: { fields: { name: "名称无效" } },
      },
    }, setError)).toBe(true);
    expect(setError).toHaveBeenCalledWith("slug", { type: "server", message: "已存在" });
    expect(setError).toHaveBeenCalledWith("name", { type: "server", message: "名称无效" });
  });

  it("returns false for form-level failures", () => {
    expect(applyApiFieldErrors({ error: { message: "服务不可用" } }, vi.fn())).toBe(false);
  });
});

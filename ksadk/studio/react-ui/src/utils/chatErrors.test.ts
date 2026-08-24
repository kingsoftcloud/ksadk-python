import { describe, expect, it } from "vitest";
import { redactTechnicalError, runErrorCopy } from "./chatErrors";

describe("chat run errors", () => {
  it("maps credential and model failures to recoverable Chinese guidance", () => {
    expect(runErrorCopy("401 invalid API key")).toMatchObject({
      title: "模型凭证无效或已过期",
      recoverable: "credential",
    });
    expect(runErrorCopy("must configure model before running")).toMatchObject({
      title: "当前 Agent 尚未绑定模型",
      recoverable: "model",
    });
  });

  it("redacts secrets while retaining trace identifiers", () => {
    const value = redactTechnicalError("Authorization: Bearer sk-secretvalue run_1234567890abcdef1234567890");
    expect(value).not.toContain("secretvalue");
    expect(value).toContain("run_1234567890abcdef1234567890");
  });
});

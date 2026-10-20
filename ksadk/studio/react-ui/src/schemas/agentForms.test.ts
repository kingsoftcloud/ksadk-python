import { describe, expect, it } from "vitest";
import { agentImportSchema, conversationCommitSchema, quickAgentSchema } from "./agentForms";

describe("agent authoring schemas", () => {
  it("distinguishes required, optional and generated fields", () => {
    const result = quickAgentSchema.safeParse({
      name: "",
      slug: "Bad Slug",
      runtimeType: "codex",
      prompt: "x",
      description: "",
    });
    expect(result.success).toBe(false);
    if (!result.success) {
      expect(result.error.flatten().fieldErrors.name).toContain("请填写 Agent 名称");
      expect(result.error.flatten().fieldErrors.slug).toContain("本地标识只能包含小写字母、数字和连字符");
      expect(result.error.flatten().fieldErrors.prompt).toContain("Agent 目标与要求至少填写 4 个字符");
    }
  });

  it("accepts generated local ids and optional descriptions", () => {
    expect(quickAgentSchema.safeParse({
      name: "Research",
      slug: "agentkit-a1b2c3d4",
      runtimeType: "adk",
      prompt: "Help safely.",
      description: "",
    }).success).toBe(true);
    expect(conversationCommitSchema.safeParse({
      name: "Conversation",
      slug: "agentkit-0011aaff",
      runtimeType: "langgraph",
      prompt: "Answer with evidence.",
    }).success).toBe(true);
    expect(agentImportSchema.safeParse({ name: "Imported", slug: "" }).success).toBe(true);
  });

  it("accepts a Native coding template with no overall deadline", () => {
    expect(quickAgentSchema.safeParse({
      name: "Native developer", slug: "native-developer", runtimeType: "harness",
      template: "coding", prompt: "Implement and verify the requested project.",
      timeoutSeconds: 0,
    }).success).toBe(true);
  });

  it("requires a target audience only for the research template", () => {
    const result = quickAgentSchema.safeParse({
      name: "Research",
      slug: "agentkit-a1b2c3d4",
      runtimeType: "codex",
      template: "research",
      prompt: "Research safely.",
      description: "",
      audience: "",
    });
    expect(result.success).toBe(false);
    if (!result.success) {
      expect(result.error.flatten().fieldErrors.audience).toContain("请填写目标读者");
    }
  });
});

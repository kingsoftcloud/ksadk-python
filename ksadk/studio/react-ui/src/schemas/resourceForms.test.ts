import { describe, expect, it } from "vitest";
import {
  credentialValueSchema,
  mcpSchema,
  modelProfileSchema,
  pythonToolSchema,
  settingsSchema,
} from "./resourceForms";

describe("resource schemas", () => {
  it("validates endpoint URLs and Python identifiers", () => {
    expect(modelProfileSchema.safeParse({
      name: "glm",
      displayName: "GLM",
      model: "glm-5.2",
      endpointUrl: "not-a-url",
      credentialRef: "env://MODEL_KEY",
    }).success).toBe(false);
    expect(pythonToolSchema.safeParse({
      displayName: "Tool",
      name: "bad-name",
      callableName: "run",
      description: "Run",
    }).success).toBe(false);
  });

  it("accepts valid MCP and model forms", () => {
    expect(modelProfileSchema.safeParse({
      name: "glm-5.2",
      displayName: "GLM 5.2",
      model: "glm-5.2",
      endpointUrl: "https://example.test/v1/chat/completions",
      credentialRef: "env://MODEL_KEY",
    }).success).toBe(true);
    expect(mcpSchema.safeParse({
      displayName: "Search MCP",
      name: "search-mcp",
      transport: "streamable-http",
      endpointUrl: "https://mcp.example.test",
      description: "",
    }).success).toBe(true);
  });

  it("validates model tuning, credential refs and transport-specific MCP fields", () => {
    const invalidModel = modelProfileSchema.safeParse({
      name: "glm-5.2",
      displayName: "GLM 5.2",
      model: "glm-5.2",
      endpointUrl: "https://example.test/v1/responses",
      credentialRef: "MODEL KEY",
      temperature: 3,
      maxTokens: 0,
    });
    expect(invalidModel.success).toBe(false);
    if (!invalidModel.success) {
      expect(invalidModel.error.flatten().fieldErrors.credentialRef).toContain("凭证引用需使用 env://环境变量名");
      expect(invalidModel.error.flatten().fieldErrors.temperature).toContain("temperature 需在 0-2 之间");
      expect(invalidModel.error.flatten().fieldErrors.maxTokens).toContain("max_tokens 需为 1-131072 的整数");
    }

    const invalidMcp = mcpSchema.safeParse({
      displayName: "Local MCP",
      name: "local-mcp",
      transport: "stdio",
      command: "",
      endpointUrl: "",
      description: "",
    });
    expect(invalidMcp.success).toBe(false);
    if (!invalidMcp.success) {
      expect(invalidMcp.error.flatten().fieldErrors.command).toContain("请填写 Command");
    }
  });

  it("accepts one-character Python identifiers", () => {
    expect(pythonToolSchema.safeParse({
      displayName: "X",
      name: "x",
      callableName: "f",
      description: "",
    }).success).toBe(true);
  });

  it("requires a source path only for workspace Python tools", () => {
    const result = pythonToolSchema.safeParse({
      displayName: "Workspace Tool",
      name: "x",
      callableName: "f",
      description: "",
      sourceMode: "workspace",
      sourcePath: "",
    });
    expect(result.success).toBe(false);
    if (!result.success) {
      expect(result.error.flatten().fieldErrors.sourcePath).toContain("请填写工作区 Python 文件");
    }
  });

  it("keeps credential and settings secrets bounded without requiring persisted values", () => {
    expect(credentialValueSchema.safeParse({ value: "" }).success).toBe(true);
    expect(credentialValueSchema.safeParse({ value: "x".repeat(16_385) }).success).toBe(false);
    expect(settingsSchema.safeParse({
      sandbox: "workspace-write",
      buildAfterCreate: true,
      codexProxy: "auto",
      cloudAccessKey: "",
      cloudSecretKey: "",
      cloudRegion: "cn-beijing-6",
    }).success).toBe(true);
  });
});

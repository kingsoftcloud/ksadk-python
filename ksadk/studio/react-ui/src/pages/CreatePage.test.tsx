import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { apiFetch } from "../api";
import { CreatePage } from "./CreatePage";

vi.mock("../api", () => ({ apiFetch: vi.fn() }));

const mockedFetch = vi.mocked(apiFetch);

const model = {
  resourceId: "model-local-test",
  kind: "model",
  name: "local-test-model",
  displayName: "Local Test Model",
  version: "1.0.0",
  status: "ready",
  source: "local",
  contract: {
    model: "test-model",
    credentialRef: "env://OPENAI_API_KEY",
  },
  requiredSecretRefs: ["env://OPENAI_API_KEY"],
};

function response(payload: unknown): Response {
  return { ok: true, json: async () => payload } as Response;
}

describe("CreatePage quick authoring", () => {
  beforeEach(() => {
    mockedFetch.mockReset();
    mockedFetch.mockImplementation(async (input) => {
      const path = String(input);
      if (path === "/api/v1/catalog/resources?limit=200") {
        return response({ items: [model] });
      }
      if (path === "/api/v1/catalog/models") {
        return response({ items: [] });
      }
      if (path === "/api/v1/credentials/OPENAI_API_KEY") {
        return response({ configured: true });
      }
      if (path === "/api/v1/agent-templates/blank:compose") {
        return response({
          templateId: "blank",
          spec: {
            instructions: { system: "Composed system prompt.", task: "" },
            bindings: {
              modelProfileId: model.resourceId,
              modelProfileIds: [model.resourceId],
              tools: [],
              skills: [],
              mcpServers: [],
            },
          },
        });
      }
      if (path === "/api/v1/authoring/quick") {
        return response({ metadata: { id: "codex-local-test", revision: 1 } });
      }
      if (path === "/api/v1/agents/codex-local-test/builds") {
        return response({ id: "build-operation" });
      }
      if (path === "/api/v1/operations/build-operation") {
        return response({
          status: "SUCCEEDED",
          resourceId: "build-local-test",
        });
      }
      throw new Error(`unexpected request: ${path}`);
    });
  });

  it("validates a YAML declaration before opening its local chat without claiming a code bundle", async () => {
    const user = userEvent.setup();
    const onCreated = vi.fn();
    render(
      <CreatePage
        viewportMode="desktop"
        onBack={vi.fn()}
        onCreated={onCreated}
      />,
    );

    await user.type(
      screen.getByPlaceholderText(/你是一名企业技术支持助手/),
      "你是一个本地验证助手，请简洁回答。",
    );
    await user.click(screen.getByRole("button", { name: "继续" }));
    await screen.findByRole("button", { name: "选择模型" });
    await user.click(screen.getByRole("button", { name: "选择模型" }));
    await user.click(screen.getByRole("option", { name: /Local Test Model/ }));
    await user.keyboard("{Escape}");
    await user.click(screen.getByRole("button", { name: "继续" }));

    await waitFor(() => {
      const call = mockedFetch.mock.calls.find(
        ([path]) => path === "/api/v1/agent-templates/blank:compose",
      );
      expect(call).toBeDefined();
      const request = JSON.parse(String(call?.[1]?.body));
      expect(request).toEqual({
        prompt: "你是一个本地验证助手，请简洁回答。",
        goal: "你是一个本地验证助手，请简洁回答。",
        description: "",
        taskPrompt: "",
        audience: "产品与技术负责人",
        language: "zh-CN",
        depth: "deep",
        outputFormat: "report",
        modelProfileId: "model-local-test",
        modelProfileIds: ["model-local-test"],
        toolResourceIds: [],
        skillResourceIds: [],
        mcpResourceIds: [],
        policyTemplate: "strict",
        executionStrategy: "direct",
        maxSteps: 12,
        timeoutSeconds: 120,
      });
    });

    await user.click(screen.getByRole("button", { name: "继续" }));
    expect(screen.getByText("创建后立即校验 YAML 声明并打开会话")).toBeInTheDocument();
    expect(screen.getByText("只冻结 YAML 和 runtime 摘要；部署时不会上传代码包。")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "创建 Agent" }));

    await waitFor(() => {
      expect(mockedFetch).toHaveBeenCalledWith(
        "/api/v1/authoring/quick",
        expect.objectContaining({ method: "POST" }),
      );
      expect(mockedFetch).toHaveBeenCalledWith(
        "/api/v1/agents/codex-local-test/builds",
        expect.objectContaining({ method: "POST" }),
      );
      expect(onCreated).toHaveBeenCalledWith("codex-local-test", true);
    });
  });

  it("confirms a conversation proposal without dropping the complete AgentSpec", async () => {
    const user = userEvent.setup();
    const onCreated = vi.fn();
    const proposal = {
      name: "Release Agent",
      slug: "release-agent",
      runtimeType: "langgraph",
      description: "Release review",
      spec: {
        description: "Release review",
        runtime: {
          type: "langgraph",
          projectPath: "generated/source",
          entryPoint: "main.py",
          agentVariable: "graph",
        },
        instructions: { system: "Review releases.", task: "Return evidence." },
        model: {
          model: "test-model",
          baseUrl: "https://models.example.test/v1",
          credentialRef: "env://MODEL_KEY",
          parameters: { temperature: 0.1, maxTokens: 8192 },
        },
        bindings: {
          modelProfileId: model.resourceId,
          modelProfileIds: [model.resourceId],
          modelParameters: { temperature: 0.3, maxTokens: 4096 },
          policyTemplate: "custom",
          tools: [{ resourceId: "tool-release", approval: "policy" }],
          mcpServers: [{ resourceId: "mcp-release" }],
          skills: [{ resourceId: "skill-release" }],
        },
        execution: { strategy: "plan-act-observe", maxSteps: 24, timeoutSeconds: 300 },
        context: { ownership: "framework", maxInputTokens: 64000, reserveOutputTokens: 4096 },
        memory: { enabled: true, providerRef: "memory-release" },
        security: { toolPolicy: "allow-listed", allowedPermissions: ["repo:read"] },
        evaluation: { suiteRefs: ["release-suite"], minimumPassRate: 0.9 },
      },
    };
    mockedFetch.mockImplementation(async (input) => {
      const path = String(input);
      if (path === "/api/v1/catalog/resources?limit=200") return response({ items: [model] });
      if (path === "/api/v1/catalog/models") return response({ items: [] });
      if (path === "/api/v1/credentials/OPENAI_API_KEY") return response({ configured: true });
      if (path === "/api/v1/authoring/conversations:compose") {
        return response({ proposal, requiresConfirmation: true });
      }
      if (path === "/api/v1/authoring/quick") {
        return response({ metadata: { id: "release-agent-created", revision: 1 } });
      }
      throw new Error(`unexpected request: ${path}`);
    });
    render(
      <CreatePage
        viewportMode="desktop"
        onBack={vi.fn()}
        onCreated={onCreated}
      />,
    );

    await user.click(screen.getByRole("tab", { name: /对话构建/ }));
    await user.type(
      screen.getByPlaceholderText(/做一个 ADK 发布评审 Agent/),
      "做一个完整的 LangGraph 发布评审 Agent",
    );
    await user.click(screen.getByRole("button", { name: "生成方案" }));
    await screen.findByDisplayValue("Review releases.");
    await user.click(screen.getByRole("button", { name: "确认并创建 Revision" }));

    await waitFor(() => {
      const call = mockedFetch.mock.calls.find(([path]) => path === "/api/v1/authoring/quick");
      expect(call).toBeDefined();
      const request = JSON.parse(String(call?.[1]?.body));
      expect(request.runtimeType).toBe("langgraph");
      expect(request.spec.runtime.entryPoint).toBe("main.py");
      expect(request.spec.instructions.task).toBe("Return evidence.");
      expect(request.spec.model.parameters.maxTokens).toBe(8192);
      expect(request.spec.bindings.tools).toEqual([{ resourceId: "tool-release", approval: "policy" }]);
      expect(request.spec.bindings.mcpServers).toEqual([{ resourceId: "mcp-release" }]);
      expect(request.spec.bindings.skills).toEqual([{ resourceId: "skill-release" }]);
      expect(request.spec.bindings.modelParameters.maxTokens).toBe(4096);
      expect(request.spec.bindings.policyTemplate).toBe("custom");
      expect(request.spec.execution.maxSteps).toBe(24);
      expect(request.spec.context.maxInputTokens).toBe(64000);
      expect(request.spec.memory.providerRef).toBe("memory-release");
      expect(request.spec.security.allowedPermissions).toEqual(["repo:read"]);
      expect(request.spec.evaluation.suiteRefs).toEqual(["release-suite"]);
      expect(onCreated).toHaveBeenCalledWith("release-agent-created");
    });
  });
});

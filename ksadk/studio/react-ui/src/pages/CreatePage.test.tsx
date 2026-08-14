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
  return {
    ok: true,
    json: async () => payload,
    text: async () => JSON.stringify(payload),
  } as Response;
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
      if (path === "/api/v1/agents/agentkit-edit-test") {
        return response({
          draft: {
            metadata: { id: "agentkit-edit-test", name: "Edit Test", revision: 1 },
            spec: {
              runtime: { type: "langgraph", projectPath: ".", entryPoint: "graph.py", agentVariable: "app" },
              instructions: { system: "你是一个测试助手。", task: "" },
              bindings: { modelProfileId: model.resourceId, modelProfileIds: [model.resourceId] },
              context: { ownership: "ksadk", rollout: { contextEngine: "shadow", memoryWrite: "off" } },
              memory: { enabled: false, recall: { enabled: false } },
            },
          },
        });
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

  it("composes, creates, and builds the selected runtime before opening its chat", async () => {
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
      screen.getByPlaceholderText(/面向企业运维人员处理技术支持问题/),
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

    await user.click(screen.getByText("运行上下文与长期记忆"));
    await user.click(screen.getByRole("checkbox", { name: /启用跨会话记忆/ }));
    await user.click(screen.getByRole("button", { name: "创建 Agent" }));

    await waitFor(() => {
      const createCall = mockedFetch.mock.calls.find(
        ([path]) => path === "/api/v1/authoring/quick",
      );
      const createRequest = JSON.parse(String(createCall?.[1]?.body));
      expect(createRequest.spec.context.rollout.memoryWrite).toBe("enabled");
      expect(createRequest.spec.memory.enabled).toBe(true);
      expect(createRequest.spec.memory.recall.enabled).toBe(true);
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

  it("uses three freely switchable sections when editing an Agent", async () => {
    const user = userEvent.setup();
    render(
      <CreatePage
        editingAgentId="agentkit-edit-test"
        viewportMode="desktop"
        onBack={vi.fn()}
        onCreated={vi.fn()}
      />,
    );

    const basics = await screen.findByRole("button", { name: /基础与 Prompt/ });
    const capabilities = screen.getByRole("button", { name: /能力绑定/ });
    const policy = screen.getByRole("button", { name: /运行策略/ });
    expect(basics).toBeEnabled();
    expect(capabilities).toBeEnabled();
    expect(policy).toBeEnabled();
    expect(await screen.findByRole("region", { name: "基础与 Prompt" })).toBeVisible();

    await user.click(capabilities);
    expect(screen.getByRole("region", { name: "能力绑定" })).toBeVisible();
    expect(screen.getByText("绑定模型")).toBeVisible();

    await user.click(policy);
    expect(screen.getByRole("region", { name: "运行策略" })).toBeVisible();
    expect(screen.getByText("跨会话记忆")).toBeVisible();
    expect(screen.queryByText("检查并创建")).not.toBeInTheDocument();
  });
});

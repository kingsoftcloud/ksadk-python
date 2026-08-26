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

  it("uses the global Agent breadcrumb instead of duplicating a back action in the header", () => {
    render(
      <CreatePage
        viewportMode="desktop"
        onBack={vi.fn()}
        onCreated={vi.fn()}
      />,
    );

    expect(screen.queryByRole("button", { name: "返回 Agent" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "保存草稿" }).closest(".wizard-actions")).not.toBeNull();
    expect(screen.getByRole("button", { name: "继续" }).closest(".wizard-actions")).not.toBeNull();
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
    expect(screen.queryByRole("button", { name: "配置凭证" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "继续" }));

    await waitFor(() => {
      const call = mockedFetch.mock.calls.find(
        ([path]) => path === "/api/v1/agent-templates/blank:compose",
      );
      expect(call).toBeDefined();
      const request = JSON.parse(String(call?.[1]?.body));
      expect(request).toEqual({
        prompt: "",
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

    expect(screen.getByRole("button", { name: "一键优化 Prompt" })).toBeVisible();
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

  it("uses the authoring model to optimize both the system prompt and task contract", async () => {
    const user = userEvent.setup();
    mockedFetch.mockImplementation(async (input) => {
      const path = String(input);
      if (path === "/api/v1/catalog/resources?limit=200") return response({ items: [model] });
      if (path === "/api/v1/catalog/models") return response({ items: [] });
      if (path === "/api/v1/credentials/OPENAI_API_KEY") return response({ configured: true });
      if (path === "/api/v1/agent-templates/blank:compose") {
        return response({
          templateId: "blank",
          spec: {
            description: "销售日报",
            instructions: {
              system: "你是销售助手。",
              task: "生成销售日报。",
            },
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
      if (path === "/api/v1/authoring/conversations:compose") {
        return response({
          proposal: {
            name: "销售日报 Agent",
            description: "提取事实并形成可执行日报",
            spec: {
              instructions: {
                system: "你是严谨的销售日报分析助手，只依据输入事实工作。",
                task: "按新增、风险、待跟进三段输出；缺失信息明确标注未提及。",
              },
            },
          },
          requiresConfirmation: true,
        });
      }
      throw new Error(`unexpected request: ${path}`);
    });

    render(
      <CreatePage
        viewportMode="desktop"
        onBack={vi.fn()}
        onCreated={vi.fn()}
      />,
    );

    const nameInput = screen.getByRole("textbox", { name: /Agent 名称.*必填/ });
    await user.clear(nameInput);
    await user.type(nameInput, "销售日报 Agent");
    await user.type(
      screen.getByPlaceholderText(/你是一名企业技术支持助手/),
      "根据聊天记录生成销售日报，不得编造。",
    );
    await user.click(screen.getByRole("button", { name: "继续" }));
    await screen.findByRole("button", { name: "选择模型" });
    await user.click(screen.getByRole("button", { name: "选择模型" }));
    await user.click(screen.getByRole("option", { name: /Local Test Model/ }));
    await user.keyboard("{Escape}");
    await user.click(screen.getByRole("button", { name: "继续" }));

    await screen.findByDisplayValue("你是销售助手。");
    await user.click(screen.getByRole("button", { name: "一键优化 Prompt" }));

    await waitFor(() => {
      expect(mockedFetch).toHaveBeenCalledWith(
        "/api/v1/authoring/conversations:compose",
        expect.objectContaining({ method: "POST" }),
      );
      expect(screen.getByDisplayValue("你是严谨的销售日报分析助手，只依据输入事实工作。")).toBeVisible();
      expect(screen.getByDisplayValue("按新增、风险、待跟进三段输出；缺失信息明确标注未提及。")).toBeVisible();
    });
    const optimizeCall = mockedFetch.mock.calls.find(
      ([path]) => path === "/api/v1/authoring/conversations:compose",
    );
    const optimizeRequest = JSON.parse(String(optimizeCall?.[1]?.body));
    expect(optimizeRequest.modelProfileId).toBe(model.resourceId);
    expect(optimizeRequest.messages[0].content).toContain("当前角色与系统提示词：你是销售助手。");
    expect(optimizeRequest.messages[0].content).toContain("当前任务契约：生成销售日报。");
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
          metadata: { pricing: { prompt: "1.0元", completion: "2.0元" } },
          discovery: { endpoint: "https://models.example.test/v1/models" },
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
      screen.getByPlaceholderText(/描述你想创建或调整的 Agent/),
      "做一个完整的 LangGraph 发布评审 Agent",
    );
    await user.click(screen.getByRole("button", { name: "生成方案" }));
    await screen.findByRole("button", { name: "编辑并创建" });
    expect(screen.getByText("Review releases.")).toBeVisible();
    expect(screen.getByText("Return evidence.")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "编辑并创建" }));
    await screen.findByDisplayValue("Review releases.");
    expect(screen.queryByText(/1\.0元/)).not.toBeInTheDocument();
    expect(screen.queryByText(/models\.example\.test\/v1\/models/)).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "确认并创建 Revision" }));

    await waitFor(() => {
      const call = mockedFetch.mock.calls.find(([path]) => path === "/api/v1/authoring/quick");
      expect(call).toBeDefined();
      const request = JSON.parse(String(call?.[1]?.body));
      expect(request.runtimeType).toBe("codex");
      expect(request.spec.runtime).toBeNull();
      expect(request.spec.instructions.task).toBe("Return evidence.");
      expect(request.spec.model).toBeNull();
      expect(request.spec.bindings.tools).toEqual([]);
      expect(request.spec.bindings.mcpServers).toEqual([]);
      expect(request.spec.bindings.skills).toEqual([]);
      expect(request.spec.bindings.modelParameters).toBeNull();
      expect(request.spec.bindings.policyTemplate).toBe("strict");
      expect(request.spec.execution.maxSteps).toBe(24);
      expect(request.spec.context.maxInputTokens).toBe(64000);
      expect(request.spec.memory.providerRef).toBe("memory-release");
      expect(request.spec.security.allowedPermissions).toEqual(["repo:read"]);
      expect(request.spec.evaluation.suiteRefs).toEqual(["release-suite"]);
      expect(onCreated).toHaveBeenCalledWith("release-agent-created");
    });
  });

  it("makes a server-returned local fallback explicit and keeps it editable", async () => {
    const user = userEvent.setup();
    mockedFetch.mockImplementation(async (input) => {
      const path = String(input);
      if (path === "/api/v1/catalog/resources?limit=200") return response({ items: [model] });
      if (path === "/api/v1/catalog/models") return response({ items: [] });
      if (path === "/api/v1/credentials/OPENAI_API_KEY") return response({ configured: true });
      if (path === "/api/v1/authoring/conversations:compose") {
        return response({
          proposal: {
            name: "待确认 Agent",
            slug: "conversation-agent",
            runtimeType: "codex",
            description: "做一个发布评审 Agent",
            spec: { instructions: { system: "根据用户需求完成任务。", task: "做一个发布评审 Agent" } },
          },
          requiresConfirmation: true,
          authoringMode: "local-fallback",
          fallback: { active: true, reason: "model-request-failed" },
        });
      }
      throw new Error(`unexpected request: ${path}`);
    });

    render(<CreatePage viewportMode="desktop" onBack={vi.fn()} onCreated={vi.fn()} />);
    await user.click(screen.getByRole("tab", { name: /对话构建/ }));
    await user.type(screen.getByPlaceholderText(/描述你想创建或调整的 Agent/), "做一个发布评审 Agent");
    await user.click(screen.getByRole("button", { name: "生成方案" }));

    expect(await screen.findByRole("status")).toHaveTextContent("已生成本地兜底草稿");
    await user.click(screen.getByRole("button", { name: "编辑并创建" }));
    expect(screen.getByDisplayValue("根据用户需求完成任务。")).toBeVisible();
  });

  it("keeps deployment details out of the initial conversation surface", async () => {
    const user = userEvent.setup();
    render(
      <CreatePage viewportMode="desktop" onBack={vi.fn()} onCreated={vi.fn()} />,
    );

    await user.click(screen.getByRole("tab", { name: /对话构建/ }));
    await waitFor(() => {
      expect(screen.getByText("从对话开始")).toBeVisible();
      expect(screen.getByText(/Codex · 1 个模型/)).toBeVisible();
    });
    expect(screen.queryByRole("region", { name: "draft-patch.json 源码" })).not.toBeInTheDocument();
  });
});

describe("CreatePage conversation authoring stages", () => {
  const proposal = {
    name: "Stage Agent",
    slug: "stage-agent",
    runtimeType: "codex",
    description: "Staged",
    spec: {
      description: "Staged",
      instructions: { system: "Help.", task: "" },
      bindings: { modelProfileId: model.resourceId, modelProfileIds: [model.resourceId] },
    },
  };

  it("shows staged shimmer text while composing and blocks duplicate submits", async () => {
    const user = userEvent.setup();
    let releaseCompose: ((value: Response) => void) | undefined;
    const stages = new Map<string, string>();
    mockedFetch.mockReset();
    mockedFetch.mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      if (path === "/api/v1/catalog/resources?limit=200") return response({ items: [model] });
      if (path === "/api/v1/catalog/models") return response({ items: [] });
      if (path === "/api/v1/credentials/OPENAI_API_KEY") return response({ configured: true });
      if (path.startsWith("/api/v1/authoring/conversations:status/")) {
        const requestId = path.split(":status/")[1];
        const stage = stages.get(requestId);
        if (!stage) throw new Error("unexpected status request before compose");
        return response({ requestId, stage, updatedAt: 1 });
      }
      if (path === "/api/v1/authoring/conversations:compose") {
        const body = JSON.parse(String(init?.body));
        expect(body.requestId).toEqual(expect.stringMatching(/^conv-/));
        expect(body.runtimeType).toBe("codex");
        stages.set(body.requestId, "validating");
        return new Promise<Response>(resolve => {
          releaseCompose = resolve;
        });
      }
      throw new Error(`unexpected request: ${path}`);
    });

    render(
      <CreatePage viewportMode="desktop" onBack={vi.fn()} onCreated={vi.fn()} />,
    );

    await user.click(screen.getByRole("tab", { name: /对话构建/ }));
    await user.type(
      screen.getByPlaceholderText(/描述你想创建或调整的 Agent/),
      "做一个阶段反馈 Agent",
    );
    await user.click(screen.getByRole("button", { name: "生成方案" }));

    // 阶段轮询已把等待文案推进到第二段。
    expect(screen.getByTestId("authoring-stage-shimmer").textContent).toContain("正在理解你的需求…");
    // 阶段轮询已把等待文案推进到第二段。
    const shimmer = await screen.findByText("正在校验配置…", undefined, { timeout: 4000 });
    expect(shimmer.closest("[data-stage]")).toHaveAttribute("data-stage", "validating");

    // 创建中禁止重复提交。
    expect(screen.getByRole("button", { name: "正在生成" })).toBeDisabled();

    releaseCompose?.(response({ proposal, requiresConfirmation: true }));
    await screen.findByRole("button", { name: "编辑并创建" }, { timeout: 4000 });
    await user.click(screen.getByRole("button", { name: "编辑并创建" }));
    await screen.findByDisplayValue("Help.", undefined, { timeout: 4000 });
  }, 15000);

  it("sends with Enter while preserving Shift+Enter for a line break", async () => {
    const user = userEvent.setup();
    let requestBody: any;
    mockedFetch.mockReset();
    mockedFetch.mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      if (path === "/api/v1/catalog/resources?limit=200") return response({ items: [model] });
      if (path === "/api/v1/catalog/models") return response({ items: [] });
      if (path === "/api/v1/credentials/OPENAI_API_KEY") return response({ configured: true });
      if (path === "/api/v1/authoring/conversations:compose") {
        requestBody = JSON.parse(String(init?.body));
        return response({ proposal, requiresConfirmation: true });
      }
      throw new Error(`unexpected request: ${path}`);
    });
    render(<CreatePage viewportMode="desktop" onBack={vi.fn()} onCreated={vi.fn()} />);
    await user.click(screen.getByRole("tab", { name: /对话构建/ }));
    const input = screen.getByPlaceholderText(/描述你想创建或调整的 Agent/);
    await user.type(input, "第一行");
    await user.keyboard("{Shift>}{Enter}{/Shift}第二行");
    expect(input).toHaveValue("第一行\n第二行");
    await user.keyboard("{Enter}");
    await waitFor(() => expect(requestBody?.messages.at(-1)?.content).toBe("第一行\n第二行"));
  });
});

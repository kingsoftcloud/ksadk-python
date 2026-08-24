import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { AgentEditor } from "./AgentEditor";
import { apiFetch } from "../api";

vi.mock("../api", () => ({ apiFetch: vi.fn() }));
vi.mock("../components/Toast", () => ({ showToast: vi.fn() }));

const mockedFetch = vi.mocked(apiFetch);

describe("AgentEditor form", () => {
  beforeEach(() => {
    mockedFetch.mockReset();
    mockedFetch.mockResolvedValue({
      ok: true,
      json: async () => ({
        draft: {
          metadata: { id: "agentkit-a1b2c3d4", name: "Research", revision: 1 },
          spec: {
            runtime: { type: "codex" },
            instructions: { system: "" },
            bindings: {},
          },
        },
      }),
    } as Response);
  });

  it("shows the shared prompt validation without sending an invalid update", async () => {
    render(<AgentEditor agentId="agentkit-a1b2c3d4" catalog={[]} onSaved={vi.fn()} />);
    const submit = await screen.findByRole("button", { name: "保存修改" });
    const form = submit.closest("form");
    expect(form).not.toBeNull();

    fireEvent.submit(form!);

    expect(await screen.findByRole("alert")).toHaveTextContent("系统提示词至少填写 4 个字符");
    await waitFor(() => expect(mockedFetch).toHaveBeenCalledTimes(1));
  });

  it("saves a selected icon and color as revisioned Agent appearance", async () => {
    mockedFetch.mockImplementation(async (input) => {
      const url = String(input);
      if (url.endsWith("/appearance")) {
        return {
          ok: true,
          json: async () => ({
            metadata: {
              id: "agentkit-a1b2c3d4",
              name: "Research",
              revision: 2,
              appearance: { icon: "sparkles", color: "#7c5cc4", imageUrl: null },
            },
          }),
        } as Response;
      }
      return {
        ok: true,
        json: async () => ({
          draft: {
            metadata: {
              id: "agentkit-a1b2c3d4",
              name: "Research",
              revision: 1,
              appearance: { icon: "bot", color: "#426ea8", imageUrl: null },
            },
            spec: {
              runtime: { type: "codex" },
              instructions: { system: "你是一个研究助手。" },
              bindings: {},
            },
          },
        }),
      } as Response;
    });

    render(<AgentEditor agentId="agentkit-a1b2c3d4" catalog={[]} onSaved={vi.fn()} />);
    await screen.findByRole("button", { name: "使用 Sparkles 图标" });
    fireEvent.click(screen.getByRole("button", { name: "使用 Sparkles 图标" }));
    fireEvent.click(screen.getByRole("button", { name: "使用紫罗兰配色" }));
    fireEvent.click(screen.getByRole("button", { name: "保存外观" }));

    await waitFor(() => expect(mockedFetch).toHaveBeenCalledWith(
      "/api/v1/agents/agentkit-a1b2c3d4/appearance",
      expect.objectContaining({
        method: "PUT",
        headers: expect.objectContaining({ "If-Match": "1" }),
        body: JSON.stringify({ icon: "sparkles", color: "#7c5cc4", imageUrl: null }),
      }),
    ));
  });

  it("persists an inferred model binding for an existing LangGraph agent", async () => {
    mockedFetch.mockImplementation(async (input, init) => {
      const url = String(input);
      if (init?.method === "PUT") {
        return {
          ok: true,
          json: async () => ({
            metadata: { id: "agentkit-a1b2c3d4", name: "Research", revision: 2 },
          }),
        } as Response;
      }
      return {
        ok: true,
        json: async () => ({
          draft: {
            metadata: {
              id: "agentkit-a1b2c3d4",
              name: "Research",
              revision: 1,
              labels: { "agentkit.ksyun.com/model": "glm-5.1" },
            },
            spec: {
              runtime: { type: "langgraph", projectPath: ".", entryPoint: "graph.py", agentVariable: "app" },
              instructions: { system: "你是一个研究助手。" },
              bindings: { modelProfileIds: [] },
            },
          },
        }),
      } as Response;
    });

    render(
      <AgentEditor
        agentId="agentkit-a1b2c3d4"
        activeSection={2}
        catalog={[{
          resourceId: "model-glm-5-1",
          kind: "model",
          name: "glm-5.1",
          displayName: "glm-5.1",
          version: "1",
          status: "ready",
          contract: { model: "glm-5.1" },
        }]}
        onSaved={vi.fn()}
      />,
    );

    await screen.findByText("已选 1 个");
    const rebuild = await screen.findByRole("checkbox", { name: /保存后/ });
    fireEvent.click(rebuild);
    fireEvent.click(screen.getByRole("button", { name: "保存修改" }));

    await waitFor(() => expect(mockedFetch).toHaveBeenCalledWith(
      "/api/v1/agents/agentkit-a1b2c3d4?name=Research",
      expect.objectContaining({
        method: "PUT",
        body: expect.stringContaining('"modelProfileId":"model-glm-5-1"'),
      }),
    ));
  });

  it("normalizes an enabled legacy memory configuration to real writes when saved", async () => {
    mockedFetch.mockImplementation(async (input, init) => {
      if (init?.method === "PUT") {
        return {
          ok: true,
          json: async () => ({
            metadata: { id: "agentkit-a1b2c3d4", name: "Research", revision: 2 },
          }),
        } as Response;
      }
      return {
        ok: true,
        json: async () => ({
          draft: {
            metadata: {
              id: "agentkit-a1b2c3d4",
              name: "Research",
              revision: 1,
              labels: { "agentkit.ksyun.com/model": "glm-5.1" },
            },
            spec: {
              runtime: { type: "codex" },
              instructions: { system: "你是一个研究助手。" },
              bindings: { modelProfileIds: ["model-glm-5-1"], modelProfileId: "model-glm-5-1" },
              context: {
                ownership: "native",
                promptOwnership: "framework",
                rollout: { contextEngine: "shadow", memoryWrite: "shadow" },
              },
              memory: {
                enabled: true,
                recall: { enabled: true },
                write: { mode: "candidate" },
              },
            },
          },
        }),
      } as Response;
    });

    render(
      <AgentEditor
        agentId="agentkit-a1b2c3d4"
        activeSection={3}
        catalog={[{
          resourceId: "model-glm-5-1",
          kind: "model",
          name: "glm-5.1",
          displayName: "glm-5.1",
          version: "1",
          status: "ready",
          contract: { model: "glm-5.1" },
        }]}
        onSaved={vi.fn()}
      />,
    );

    expect(await screen.findByText("当前旧配置仅召回或观察；保存修改后将正式启用记忆写入。")).toBeVisible();
    expect(screen.getByText("运行上下文（高级）")).toBeVisible();
    fireEvent.click(screen.getByRole("checkbox", { name: /保存后/ }));
    fireEvent.click(screen.getByRole("button", { name: "保存修改" }));

    await waitFor(() => {
      const updateCall = mockedFetch.mock.calls.find(([, init]) => init?.method === "PUT");
      const spec = JSON.parse(String(updateCall?.[1]?.body));
      expect(spec.context.rollout.memoryWrite).toBe("enabled");
      expect(spec.context.ownership).toBe("native");
      expect(spec.context.rollout.contextEngine).toBe("shadow");
      expect(spec.memory.enabled).toBe(true);
      expect(spec.memory.recall.enabled).toBe(true);
      expect(spec.memory.write.mode).toBe("candidate");
    });
  });

  it("exposes the shared editor sections and preserves model, Skill, MCP and Tool bindings", async () => {
    mockedFetch.mockImplementation(async (input, init) => {
      if (init?.method === "PUT") {
        return {
          ok: true,
          json: async () => ({ metadata: { id: "agentkit-a1b2c3d4", name: "Research", revision: 2 } }),
        } as Response;
      }
      return {
        ok: true,
        json: async () => ({
          draft: {
            metadata: { id: "agentkit-a1b2c3d4", name: "Research", revision: 1 },
            spec: {
              runtime: { type: "langgraph", projectPath: ".", entryPoint: "graph.py", agentVariable: "app" },
              instructions: { system: "你是一个研究助手。" },
              bindings: {
                modelProfileId: "model-a",
                modelProfileIds: ["model-a", "model-b"],
                skills: [{ resourceId: "skill-a" }],
                mcpServers: [{ resourceId: "mcp-a" }],
                tools: [{ resourceId: "tool-a" }],
              },
            },
          },
          bindingProjection: {
            unresolvedMcpServers: [{ name: "legacy-private", reason: "not-in-resource-catalog" }],
          },
        }),
      } as Response;
    });
    const catalog = [
      { resourceId: "model-a", kind: "model", name: "model-a", displayName: "Model A", version: "1", status: "ready", contract: { model: "model-a" } },
      { resourceId: "model-b", kind: "model", name: "model-b", displayName: "Model B", version: "1", status: "ready", contract: { model: "model-b" } },
      { resourceId: "skill-a", kind: "skill", name: "skill-a", displayName: "Review Skill", version: "1", status: "ready" },
      { resourceId: "mcp-a", kind: "mcp", name: "mcp-a", displayName: "Review MCP", version: "1", status: "ready" },
      { resourceId: "mcp-new", kind: "mcp", name: "mcp-new", displayName: "New MCP", version: "1", status: "ready" },
      { resourceId: "tool-a", kind: "tool", name: "tool-a", displayName: "Review Tool", version: "1", status: "ready" },
      { resourceId: "tool-python", kind: "tool", name: "tool-python", displayName: "Python Tool", version: "1", status: "ready", contract: { executor: "python" } },
      { resourceId: "tool-mcp", kind: "tool", name: "tool-mcp", displayName: "MCP Tool", version: "1", status: "ready", contract: { executor: "mcp" } },
      { resourceId: "tool-deferred", kind: "tool", name: "tool-deferred", displayName: "Deferred Tool", version: "1", status: "ready", contract: { executor: "deferred" } },
    ];

    render(<AgentEditor agentId="agentkit-a1b2c3d4" catalog={catalog} onSaved={vi.fn()} />);

    fireEvent.click(await screen.findByRole("button", { name: "能力绑定" }));
    expect(screen.getAllByText("Model A").length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText("Model B")).toBeVisible();
    expect(screen.getByText("Review Skill")).toBeVisible();
    expect(screen.getByText("Review MCP")).toBeVisible();
    expect(screen.queryByText("New MCP")).not.toBeInTheDocument();
    expect(screen.getByText(/当前 Runtime 尚未实现 MCP 源码注入/)).toBeVisible();
    expect(screen.getByText("Review Tool")).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "选择绑定 Tool" }));
    expect(screen.getByText("Python Tool")).toBeVisible();
    expect(screen.queryByText("MCP Tool")).not.toBeInTheDocument();
    expect(screen.queryByText("Deferred Tool")).not.toBeInTheDocument();
    expect(screen.getByText(/legacy-private.*资源目录/)).toBeVisible();

    fireEvent.click(screen.getByRole("button", { name: "保存修改" }));
    await waitFor(() => {
      const updateCall = mockedFetch.mock.calls.find(([, init]) => init?.method === "PUT");
      const spec = JSON.parse(String(updateCall?.[1]?.body));
      expect(spec.bindings.modelProfileIds).toEqual(["model-a", "model-b"]);
      expect(spec.bindings.skills).toEqual([{ resourceId: "skill-a" }]);
      expect(spec.bindings.mcpServers).toEqual([{ resourceId: "mcp-a" }]);
      expect(spec.bindings.tools).toEqual([{ resourceId: "tool-a" }]);
    });
  });

  it.each(["adk", "langgraph"])(
    "round-trips unresolved bindings and runtime configuration for a historical %s Agent",
    async runtimeType => {
      mockedFetch.mockImplementation(async (_input, init) => {
        if (init?.method === "PUT") {
          return {
            ok: true,
            json: async () => ({ metadata: { id: "agentkit-a1b2c3d4", name: "Research", revision: 8 } }),
          } as Response;
        }
        return {
          ok: true,
          json: async () => ({
            draft: {
              metadata: { id: "agentkit-a1b2c3d4", name: "Research", revision: 7 },
              spec: {
                runtime: {
                  type: runtimeType,
                  projectPath: "agents/research",
                  entryPoint: runtimeType === "adk" ? "agent.py" : "workflow.py",
                  agentVariable: runtimeType === "adk" ? "root_agent" : "graph",
                  detection: "declared",
                },
                instructions: { system: "你是一个研究助手。", task: "保留任务契约。" },
                bindings: {
                  modelProfileId: "model-legacy",
                  modelProfileIds: ["model-legacy", "model-ready"],
                  modelParameters: { temperature: 0.35, maxTokens: 4096, topP: 0.8 },
                  policyTemplate: "custom",
                  skills: [{ resourceId: "skill-legacy", enabled: false, approval: "always", config: { source: "import" } }],
                  mcpServers: [{ resourceId: "mcp-legacy", enabled: true, config: { namespace: "docs" } }],
                  tools: [{ resourceId: "tool-legacy", enabled: true, approval: "policy", config: { mode: "safe" } }],
                },
                execution: {
                  strategy: "plan-act-observe",
                  maxSteps: 27,
                  timeoutSeconds: 720,
                  retry: { maxAttempts: 4, backoffSeconds: 3 },
                  sandbox: "workspace-write",
                  approvalMode: "risk",
                },
                security: { toolPolicy: "allow-listed", allowedPermissions: ["network.read"] },
                evaluation: { suiteRefs: ["release"], minimumPassRate: 0.9 },
              },
            },
          }),
        } as Response;
      });

      render(
        <AgentEditor
          agentId="agentkit-a1b2c3d4"
          activeSection={2}
          catalog={[{
            resourceId: "model-ready",
            kind: "model",
            name: "ready-model",
            displayName: "Ready Model",
            version: "1",
            status: "ready",
            contract: { model: "ready-model" },
          }]}
          onSaved={vi.fn()}
        />,
      );

      expect((await screen.findAllByText("model-legacy")).length).toBeGreaterThanOrEqual(1);
      expect(screen.getByText("skill-legacy")).toBeVisible();
      expect(screen.getByText("mcp-legacy")).toBeVisible();
      expect(screen.getByText("tool-legacy")).toBeVisible();
      fireEvent.click(screen.getByRole("button", { name: "运行策略" }));
      expect(document.getElementById("editRuntimeProjectPath")).toHaveValue("agents/research");
      expect(document.getElementById("editRuntimeEntryPoint")).toHaveValue(runtimeType === "adk" ? "agent.py" : "workflow.py");
      expect(document.getElementById("editRuntimeAgentVariable")).toHaveValue(runtimeType === "adk" ? "root_agent" : "graph");
      expect(document.getElementById("editExecutionMaxSteps")).toHaveValue(27);
      expect(document.getElementById("editExecutionTimeout")).toHaveValue(720);

      fireEvent.click(screen.getByRole("checkbox", { name: /保存后/ }));
      fireEvent.click(screen.getByRole("button", { name: "保存修改" }));
      await waitFor(() => {
        const updateCall = mockedFetch.mock.calls.find(([, init]) => init?.method === "PUT");
        const spec = JSON.parse(String(updateCall?.[1]?.body));
        expect(spec.runtime).toMatchObject({
          type: runtimeType,
          projectPath: "agents/research",
          entryPoint: runtimeType === "adk" ? "agent.py" : "workflow.py",
          agentVariable: runtimeType === "adk" ? "root_agent" : "graph",
        });
        expect(spec.instructions.task).toBe("保留任务契约。");
        expect(spec.bindings.skills).toEqual([{ resourceId: "skill-legacy", enabled: false, approval: "always", config: { source: "import" } }]);
        expect(spec.bindings.mcpServers).toEqual([{ resourceId: "mcp-legacy", enabled: true, config: { namespace: "docs" } }]);
        expect(spec.bindings.tools).toEqual([{ resourceId: "tool-legacy", enabled: true, approval: "policy", config: { mode: "safe" } }]);
        expect(spec.bindings.modelParameters).toEqual({ temperature: 0.35, maxTokens: 4096, topP: 0.8 });
        expect(spec.execution).toMatchObject({
          strategy: "plan-act-observe",
          maxSteps: 27,
          timeoutSeconds: 720,
          retry: { maxAttempts: 4, backoffSeconds: 3 },
          sandbox: "workspace-write",
          approvalMode: "risk",
        });
        expect(spec.security).toEqual({ toolPolicy: "allow-listed", allowedPermissions: ["network.read"] });
        expect(spec.evaluation).toEqual({ suiteRefs: ["release"], minimumPassRate: 0.9 });
      });
    },
  );

  it("keeps a historical Codex Tool binding visible and immutable while editing supported bindings", async () => {
    mockedFetch.mockImplementation(async (_input, init) => {
      if (init?.method === "PUT") {
        return {
          ok: true,
          json: async () => ({ metadata: { id: "agentkit-a1b2c3d4", name: "Research", revision: 3 } }),
        } as Response;
      }
      return {
        ok: true,
        json: async () => ({
          draft: {
            metadata: { id: "agentkit-a1b2c3d4", name: "Research", revision: 2 },
            spec: {
              runtime: { type: "codex", version: "0.144.4" },
              instructions: { system: "你是一个研究助手。", task: "保留任务契约。" },
              bindings: {
                modelProfileId: "model-a",
                modelProfileIds: ["model-a"],
                skills: [{ resourceId: "skill-legacy", enabled: false }],
                mcpServers: [{ resourceId: "mcp-legacy", enabled: true }],
                tools: [{ resourceId: "tool-old", enabled: true, config: { migrated: false } }],
              },
            },
          },
        }),
      } as Response;
    });

    render(<AgentEditor
      agentId="agentkit-a1b2c3d4"
      activeSection={2}
      catalog={[{ resourceId: "model-a", kind: "model", name: "model-a", displayName: "Model A", version: "1", status: "ready", contract: { model: "model-a" } }]}
      onSaved={vi.fn()}
    />);

    expect(await screen.findByText("tool-old")).toBeVisible();
    expect(screen.getByText(/当前 Runtime 不支持新增 ksadk Tool/)).toBeVisible();
    expect(screen.queryByRole("button", { name: "移除 tool-old" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("checkbox", { name: /保存后/ }));
    fireEvent.click(screen.getByRole("button", { name: "保存修改" }));
    await waitFor(() => {
      const updateCall = mockedFetch.mock.calls.find(([, init]) => init?.method === "PUT");
      const spec = JSON.parse(String(updateCall?.[1]?.body));
      expect(spec.bindings.tools).toEqual([{ resourceId: "tool-old", enabled: true, config: { migrated: false } }]);
      expect(spec.bindings.skills).toEqual([{ resourceId: "skill-legacy", enabled: false }]);
      expect(spec.bindings.mcpServers).toEqual([{ resourceId: "mcp-legacy", enabled: true }]);
    });
  });

  it("preserves a historical Codex manifest model that is not in the resource catalog", async () => {
    mockedFetch.mockImplementation(async (_input, init) => {
      if (init?.method === "PUT") {
        return {
          ok: true,
          json: async () => ({ metadata: { id: "agentkit-a1b2c3d4", name: "Research", revision: 5 } }),
        } as Response;
      }
      return {
        ok: true,
        json: async () => ({
          draft: {
            metadata: {
              id: "agentkit-a1b2c3d4",
              name: "Research",
              revision: 4,
              labels: { "agentkit.ksyun.com/model": "legacy-codex-model" },
            },
            spec: {
              runtime: { type: "codex", version: "0.144.4" },
              instructions: { system: "你是一个研究助手。", task: "" },
              bindings: { modelProfileId: null, modelProfileIds: [] },
            },
          },
        }),
      } as Response;
    });

    render(<AgentEditor
      agentId="agentkit-a1b2c3d4"
      activeSection={2}
      catalog={[]}
      onSaved={vi.fn()}
    />);

    expect(await screen.findByText(/历史声明模型 legacy-codex-model 将原样保留/)).toBeVisible();
    fireEvent.click(screen.getByRole("checkbox", { name: /保存后/ }));
    fireEvent.click(screen.getByRole("button", { name: "保存修改" }));
    await waitFor(() => {
      const updateCall = mockedFetch.mock.calls.find(([, init]) => init?.method === "PUT");
      const spec = JSON.parse(String(updateCall?.[1]?.body));
      expect(spec.bindings.modelProfileId).toBeNull();
      expect(spec.bindings.modelProfileIds).toEqual([]);
    });
  });
});

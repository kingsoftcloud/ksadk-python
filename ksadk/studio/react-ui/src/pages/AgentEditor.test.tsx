import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
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

  it("shows legacy Memory defaults without silently changing its rollout on save", async () => {
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

    expect(await screen.findByRole("group", { name: "Memory · 跨会话策略" })).toBeVisible();
    expect(screen.getByRole("textbox", { name: /Memory Provider/ })).toHaveValue("local-default");
    expect(screen.getByRole("combobox", { name: "Memory 写入 Rollout" })).toHaveTextContent("仅观察");
    expect(screen.getByText("运行上下文（高级）")).toBeVisible();
    fireEvent.click(screen.getByRole("checkbox", { name: /保存后/ }));
    fireEvent.click(screen.getByRole("button", { name: "保存修改" }));

    await waitFor(() => {
      const updateCall = mockedFetch.mock.calls.find(([, init]) => init?.method === "PUT");
      const spec = JSON.parse(String(updateCall?.[1]?.body));
      expect(spec.context.rollout.memoryWrite).toBe("shadow");
      expect(spec.context.ownership).toBe("native");
      expect(spec.context.rollout.contextEngine).toBe("shadow");
      expect(spec.memory).toEqual({
        enabled: true,
        recall: { enabled: true },
        write: { mode: "candidate" },
      });
    });
  });

  it("edits a reviewed Soul and the complete Memory policy in aligned accessible fields", async () => {
    const user = userEvent.setup();
    mockedFetch.mockImplementation(async (_input, init) => {
      if (init?.method === "PUT") {
        return {
          ok: true,
          json: async () => ({
            metadata: { id: "agentkit-a1b2c3d4", name: "Research", revision: 4 },
          }),
        } as Response;
      }
      return {
        ok: true,
        json: async () => ({
          draft: {
            metadata: { id: "agentkit-a1b2c3d4", name: "Research", revision: 3 },
            spec: {
              runtime: { type: "adk", projectPath: ".", entryPoint: "agent.py", agentVariable: "root_agent" },
              instructions: { system: "你是一个研究助手。", task: "保留任务契约。" },
              soul: {
                schemaVersion: "agentkit.soul/v1",
                identity: "可靠的研究助手",
                principles: ["先展示证据"],
                boundaries: ["不编造来源"],
                tone: "清晰、克制",
              },
              bindings: { modelProfileId: "model-a", modelProfileIds: ["model-a"] },
              context: { rollout: { contextEngine: "shadow", memoryWrite: "shadow" } },
              memory: {
                enabled: true,
                providerRef: "memory://team",
                recall: { enabled: true, maxTokens: 900, topK: 6, minScore: 0.55 },
                write: { mode: "candidate", flushBeforeCompaction: true },
              },
            },
          },
          soulProjection: {
            present: true,
            source: "AgentSpec.soul",
            sourceRevision: 3,
            digest: "sha256:abc123",
            compileTarget: "resolved-agent-spec.instructions.system",
            compileOrder: "before-instructions.system",
          },
        }),
      } as Response;
    });

    render(
      <AgentEditor
        agentId="agentkit-a1b2c3d4"
        catalog={[{
          resourceId: "model-a",
          kind: "model",
          name: "model-a",
          displayName: "Model A",
          version: "1",
          status: "ready",
          contract: { model: "model-a" },
        }]}
        onSaved={vi.fn()}
      />,
    );

    const soulGroup = await screen.findByRole("group", { name: "Soul · 稳定人格" });
    expect(soulGroup).toBeVisible();
    expect(screen.getByRole("textbox", { name: /身份定义/ })).toHaveValue("可靠的研究助手");
    expect(screen.getByRole("textbox", { name: /原则/ })).toHaveValue("先展示证据");
    expect(screen.getByRole("textbox", { name: /边界/ })).toHaveValue("不编造来源");
    expect(screen.getByRole("status", { name: "Soul 编译来源" })).toHaveTextContent("sha256:abc123");
    expect(soulGroup.querySelector(".soul-list-grid")).toHaveClass("form-grid", "two-columns");

    await user.clear(screen.getByRole("textbox", { name: /身份定义/ }));
    await user.type(screen.getByRole("textbox", { name: /身份定义/ }), "审慎的发布助手");
    await user.clear(screen.getByRole("textbox", { name: /原则/ }));
    await user.type(screen.getByRole("textbox", { name: /原则/ }), "先展示证据\n再给出建议");
    expect(screen.getByRole("status", { name: "Soul 编译来源" })).toHaveTextContent("保存后重新计算");

    await user.click(screen.getByRole("button", { name: "运行策略" }));
    const memoryGroup = screen.getByRole("group", { name: "Memory · 跨会话策略" });
    expect(memoryGroup).toBeVisible();
    expect(memoryGroup.querySelector(".memory-policy-grid")).toHaveClass("form-grid", "two-columns");
    expect(screen.getByRole("spinbutton", { name: /召回 Token 上限/ })).toHaveValue(900);
    expect(screen.getByRole("spinbutton", { name: /召回条数/ })).toHaveValue(6);
    expect(screen.getByRole("spinbutton", { name: /最小相关度/ })).toHaveValue(0.55);

    const provider = screen.getByRole("textbox", { name: /Memory Provider/ });
    await user.clear(provider);
    await user.type(provider, "memory://reviewed");
    await user.click(screen.getByRole("combobox", { name: "Memory 写入模式" }));
    await user.click(await screen.findByRole("option", { name: "仅显式写入" }));
    await user.click(screen.getByRole("combobox", { name: "Memory 写入 Rollout" }));
    await user.click(await screen.findByRole("option", { name: "正式启用" }));

    await user.click(screen.getByRole("button", { name: "基础与 Prompt" }));
    await user.click(screen.getByRole("checkbox", { name: /保存后/ }));
    await user.click(screen.getByRole("button", { name: "保存修改" }));

    await waitFor(() => {
      const updateCall = mockedFetch.mock.calls.find(([, init]) => init?.method === "PUT");
      const spec = JSON.parse(String(updateCall?.[1]?.body));
      expect(spec.soul).toEqual({
        schemaVersion: "agentkit.soul/v1",
        identity: "审慎的发布助手",
        principles: ["先展示证据", "再给出建议"],
        boundaries: ["不编造来源"],
        tone: "清晰、克制",
      });
      expect(spec.memory).toMatchObject({
        enabled: true,
        providerRef: "memory://reviewed",
        recall: { enabled: true, maxTokens: 900, topK: 6, minScore: 0.55 },
        write: { mode: "explicit_only", flushBeforeCompaction: true },
      });
      expect(spec.context.rollout.memoryWrite).toBe("enabled");
    });
  });

  it("shows the Codex Soul in the ManagedRuntime manifest and base_instructions target", async () => {
    const digest = `sha256:${"a".repeat(64)}`;
    mockedFetch.mockResolvedValue({
      ok: true,
      json: async () => ({
        draft: {
          metadata: { id: "agentkit-a1b2c3d4", name: "Research", revision: 3 },
          spec: {
            runtime: { type: "codex", version: "0.147.0" },
            instructions: { system: "Answer with evidence.", task: "" },
            soul: {
              schemaVersion: "agentkit.soul/v1",
              identity: "A careful release reviewer.",
              principles: ["Prefer evidence"],
              boundaries: ["Never expose credentials"],
              tone: "Concise and direct.",
            },
            bindings: {},
          },
        },
        soulProjection: {
          present: true,
          source: "AgentSpec.soul",
          sourceRevision: 3,
          digest,
          compileTarget: "managed-runtime.base_instructions",
          compileOrder: "before-instructions.system",
        },
      }),
    } as Response);

    render(<AgentEditor agentId="agentkit-a1b2c3d4" catalog={[]} onSaved={vi.fn()} />);

    expect(await screen.findByRole("status", { name: "Soul 编译来源" })).toHaveTextContent(
      "managed-runtime.base_instructions",
    );
    expect(screen.getByText(/ManagedRuntime 启动时会把 Soul 确定性编译到 base_instructions/)).toBeVisible();
    const manifest = screen.getByRole("region", { name: "agentkit.yaml 源码" });
    await waitFor(() => {
      expect(manifest).toHaveTextContent("version: 0.147.0");
      expect(manifest).not.toHaveTextContent("0.144.4");
      expect(manifest).toHaveTextContent("soul:");
      expect(manifest).toHaveTextContent("identity:");
      expect(manifest).toHaveTextContent("A careful release reviewer.");
      expect(manifest).toHaveTextContent("soul_source: AgentSpec.soul");
      expect(manifest).toHaveTextContent(`soul_digest: ${digest}`);
    });
  });

  it.each(["langgraph", "harness"])("preserves bindings and exposes correct MCP controls for %s", async runtimeType => {
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
              runtime: runtimeType === "harness" ? { type: "harness" } : { type: "langgraph", projectPath: ".", entryPoint: "graph.py", agentVariable: "app" },
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

    if (runtimeType === "harness") {
      fireEvent.click(await screen.findByText("本地运行：未授权 · 高级权限"));
      const consent = await screen.findByRole("checkbox", { name: /允许 KsADK Harness/ });
      expect(consent).not.toBeChecked();
      fireEvent.click(consent);
    }
    fireEvent.click(await screen.findByRole("button", { name: "能力绑定" }));
    expect(screen.getAllByText("Model A").length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText("Model B")).toBeVisible();
    expect(screen.getByText("Review Skill")).toBeVisible();
    expect(screen.getByText("Review MCP")).toBeVisible();
    expect(screen.queryByText("New MCP")).not.toBeInTheDocument();
    if (runtimeType === "harness") {
      expect(screen.getByText(/由 KsADK Harness 按需加载/)).toBeVisible();
      fireEvent.click(screen.getByRole("button", { name: "选择绑定 MCP" }));
      fireEvent.click(screen.getByRole("option", { name: /New MCP/ }));
      fireEvent.keyDown(document.activeElement!, { key: "Escape" });
    } else {
      expect(screen.getByText(/当前 Runtime 尚未实现 MCP 源码注入/)).toBeVisible();
    }
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
      expect(spec.bindings.mcpServers).toEqual(runtimeType === "harness"
        ? [{ resourceId: "mcp-a" }, { resourceId: "mcp-new", enabled: true }]
        : [{ resourceId: "mcp-a" }]);
      expect(spec.runtime.type).toBe(runtimeType);
      if (runtimeType === "harness") expect(spec.runtime.entryPoint).toBeUndefined();
      if (runtimeType === "harness") expect(spec.security.allowedPermissions).toContain("process:host-user");
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

  it("hides unsupported Codex Tool controls while preserving the historical binding", async () => {
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

    await screen.findByRole("button", { name: "保存修改" });
    expect(screen.queryByText("tool-old")).not.toBeInTheDocument();
    expect(screen.queryByText(/绑定 Tool/)).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "选择绑定 Tool" })).not.toBeInTheDocument();
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

  it("edits an installed external AgentProvider reference and secret-safe config", async () => {
    mockedFetch.mockImplementation(async (_input, init) => {
      if (init?.method === "PUT") {
        return {
          ok: true,
          json: async () => ({ metadata: { id: "agentkit-provider", name: "Provider Agent", revision: 4 } }),
        } as Response;
      }
      return {
        ok: true,
        json: async () => ({
          draft: {
            metadata: { id: "agentkit-provider", name: "Provider Agent", revision: 3 },
            spec: {
              runtime: {
                type: "plugin",
                providerRef: "plugin://io.example.provider@1.2.3",
                providerConfig: { apiKeyRef: "env://OLD_PROVIDER_KEY" },
              },
              instructions: { system: "Use the provider.", task: "" },
              bindings: { modelProfileId: "model-a", modelProfileIds: ["model-a"] },
              security: { toolPolicy: "deny-by-default", allowedPermissions: [] },
            },
          },
        }),
      } as Response;
    });

    render(<AgentEditor
      agentId="agentkit-provider"
      catalog={[{ resourceId: "model-a", kind: "model", name: "model-a", displayName: "Model A", version: "1", status: "ready", contract: { model: "model-a" } }]}
      providers={[{
        providerRef: "plugin://io.example.provider@1.2.3",
        pluginId: "io.example.provider",
        resolvedVersion: "1.2.3",
        displayName: "Example Provider",
        state: "enabled",
        compatible: true,
        selectable: true,
        reason: null,
        permissions: ["process:host-user"],
        isolation: "process",
        configSchemaDeclared: false,
        secretFields: ["apiKeyRef"],
      }]}
      onSaved={vi.fn()}
    />);

    expect(await screen.findByRole("combobox", { name: "AgentProvider" })).toHaveTextContent("Example Provider");
    const config = screen.getByRole("textbox", { name: /Provider 配置/ });
    fireEvent.change(config, { target: { value: '{"apiKeyRef":"env://NEW_PROVIDER_KEY","mode":"strict"}' } });
    fireEvent.click(screen.getByRole("checkbox", { name: /确认 Provider 请求的权限/ }));
    fireEvent.click(screen.getByRole("checkbox", { name: /保存后/ }));
    fireEvent.click(screen.getByRole("button", { name: "保存修改" }));

    await waitFor(() => {
      const updateCall = mockedFetch.mock.calls.find(([, init]) => init?.method === "PUT");
      const spec = JSON.parse(String(updateCall?.[1]?.body));
      expect(spec.runtime).toEqual({
        type: "plugin",
        providerRef: "plugin://io.example.provider@1.2.3",
        providerConfig: { apiKeyRef: "env://NEW_PROVIDER_KEY", mode: "strict" },
      });
      expect(spec.security.allowedPermissions).toEqual(["process:host-user"]);
    });
  });
});

it.each(["update", "build"])("preserves multiple model selections when %s loses its connection", async (stage) => {
  mockedFetch.mockReset();
  const draft = {
    metadata: { id: "agentkit-save", name: "Save test", revision: 1 },
    spec: {
      runtime: { type: "codex" },
      instructions: { system: "Answer with evidence." },
      bindings: { modelProfileId: "model-a", modelProfileIds: ["model-a", "model-b"] },
    },
  };
  mockedFetch.mockImplementation(async (_input, init) => {
    if (init?.method === "PUT") {
      if (stage === "update") throw new TypeError("Failed to fetch");
      return { ok: true, json: async () => ({ ...draft, metadata: { ...draft.metadata, revision: 2 } }) } as Response;
    }
    if (init?.method === "POST") throw new TypeError("Failed to fetch");
    return { ok: true, json: async () => ({ draft }) } as Response;
  });
  const onSaved = vi.fn();
  const catalog = ["a", "b"].map(id => ({ resourceId: `model-${id}`, kind: "model", name: `model-${id}`, displayName: `Model ${id}`, version: "1", status: "ready" }));
  render(<AgentEditor agentId="agentkit-save" catalog={catalog} onSaved={onSaved} />);
  fireEvent.click(await screen.findByRole("button", { name: "能力绑定" }));
  fireEvent.submit(screen.getByRole("button", { name: "保存修改" }).closest("form")!);
  expect(await screen.findByText(stage === "update" ? /尚未确认保存结果/ : /配置已保存，但后续构建未完成/)).toBeVisible();
  expect(screen.getAllByTestId("studio-multi-select-selection")[0]).toHaveTextContent("Model b");
  expect(onSaved).not.toHaveBeenCalled();
  expect(mockedFetch.mock.calls.filter(([, init]) => init?.method === "PUT")).toHaveLength(1);
});

it("saves the installed Figma plugin snapshot as an Agent binding", async () => {
  mockedFetch.mockReset();
  const snapshot = { pluginRef: "plugin://codex.figma@2.0.20", snapshotDigest: `sha256:${"a".repeat(64)}`, components: [{ id: "app:figma", kind: "app" }, { id: "skill:figma-use", kind: "skill" }] };
  mockedFetch.mockImplementation(async (input, init) => {
    const url = String(input);
    let data: unknown = { draft: { metadata: { id: "agent-test", name: "Designer", revision: 1 }, spec: { runtime: { type: "codex" }, instructions: { system: "Help with product designs." }, bindings: { modelProfileId: "model-a", modelProfileIds: ["model-a"], plugins: [] } } } };
    if (url.includes("/plugin-ecosystems/")) data = url.includes("?")
      ? { items: [{ pluginId: "figma@official", displayName: "Figma", installed: true, enabled: true }] }
      : { item: { displayName: "Figma" }, snapshot };
    if (init?.method === "PUT") data = { metadata: { id: "agent-test", revision: 2 } };
    return { ok: true, json: async () => data } as Response;
  });
  render(<AgentEditor agentId="agent-test" activeSection={2} catalog={[{ resourceId: "model-a", kind: "model", name: "model-a", displayName: "model-a", version: "1", status: "ready" }]} onSaved={vi.fn()} />);
  await userEvent.click(await screen.findByRole("button", { name: "选择绑定插件" }));
  await userEvent.click(await screen.findByRole("option", { name: /Figma/ }));
  await userEvent.keyboard("{Escape}");
  fireEvent.click(screen.getByRole("checkbox", { name: /保存后生成配置快照/ }));
  fireEvent.submit(screen.getByRole("button", { name: "保存修改" }).closest("form")!);
  await waitFor(() => {
    const put = mockedFetch.mock.calls.find(([, init]) => init?.method === "PUT");
    expect(put).toBeDefined();
    expect(JSON.parse(String(put![1]!.body)).bindings.plugins).toEqual([{ ecosystem: "codex", pluginRef: snapshot.pluginRef, snapshotDigest: snapshot.snapshotDigest, components: ["app:figma", "skill:figma-use"], enabled: true, config: {} }]);
  });
});

it("persists explicit native Codex permissions and restores consent when reopened", async () => {
  mockedFetch.mockReset();
  const provider = {
    providerRef: "plugin://io.ksadk.codex-provider@1.0.0", pluginId: "io.ksadk.codex-provider",
    resolvedVersion: "1.0.0", displayName: "Codex", state: "enabled" as const,
    compatible: true, selectable: true, permissions: ["process:host-user"], isolation: "sidecar",
    configSchemaDeclared: false, secretFields: [],
  };
  let draft = {
    metadata: { id: "native-codex", name: "Native", revision: 1 },
    spec: {
      runtime: { type: "codex", version: "0.147.0" },
      instructions: { system: "Answer with evidence." },
      bindings: { modelProfileId: "model-a", modelProfileIds: ["model-a"] },
      security: { allowedPermissions: ["filesystem:read"], toolPolicy: "deny-by-default" },
    },
  };
  mockedFetch.mockImplementation(async (_input, init) => {
    if (init?.method === "PUT") {
      draft = { metadata: { ...draft.metadata, revision: draft.metadata.revision + 1 }, spec: JSON.parse(String(init.body)) };
      return { ok: true, json: async () => draft } as Response;
    }
    return { ok: true, json: async () => ({ draft }) } as Response;
  });
  const props = {
    agentId: "native-codex", providers: [provider], onSaved: vi.fn(),
    catalog: [{ resourceId: "model-a", kind: "model", name: "model-a", displayName: "Model A", version: "1", status: "ready" }],
  };
  const view = render(<AgentEditor {...props} />);
  const consent = await screen.findByRole("checkbox", { name: /确认 Codex Provider/ });
  expect(consent).not.toBeChecked();
  fireEvent.click(screen.getByRole("checkbox", { name: /保存后/ }));
  fireEvent.click(screen.getByRole("button", { name: "保存修改" }));
  expect(await screen.findByText("请先确认 Codex Provider 请求的 Agent 权限")).toBeVisible();
  expect(mockedFetch.mock.calls.filter(([, init]) => init?.method === "PUT")).toHaveLength(0);
  fireEvent.click(consent);
  fireEvent.click(screen.getByRole("button", { name: "保存修改" }));
  await waitFor(() => expect(props.onSaved).toHaveBeenCalled());
  expect(draft.spec.runtime.type).toBe("codex");
  expect(draft.spec.security.allowedPermissions).toEqual(["filesystem:read", "process:host-user"]);
  expect(draft.spec.security.toolPolicy).toBe("deny-by-default");
  view.unmount();
  render(<AgentEditor {...props} />);
  expect(await screen.findByRole("checkbox", { name: /确认 Codex Provider/ })).toBeChecked();
  fireEvent.click(screen.getByRole("checkbox", { name: /保存后/ }));
  fireEvent.click(screen.getByRole("button", { name: "保存修改" }));
  await waitFor(() => expect(props.onSaved).toHaveBeenCalledTimes(2));
  expect(draft.spec.security.allowedPermissions).toEqual(["filesystem:read", "process:host-user"]);
});

it("projects late Provider permissions without reloading or overwriting dirty Agent fields", async () => {
  mockedFetch.mockReset();
  const provider = {
    providerRef: "plugin://io.ksadk.codex-provider@1.0.0", pluginId: "io.ksadk.codex-provider",
    resolvedVersion: "1.0.0", displayName: "Codex", state: "enabled" as const,
    compatible: true, selectable: true, permissions: ["process:host-user"], isolation: "sidecar",
    configSchemaDeclared: false, secretFields: [],
  };
  mockedFetch.mockResolvedValue({ ok: true, json: async () => ({ draft: {
    metadata: { id: "late-provider", name: "Late", revision: 1 },
    spec: {
      runtime: { type: "codex" }, instructions: { system: "Original instructions." },
      bindings: { modelProfileId: "model-a", modelProfileIds: ["model-a"] },
      security: { allowedPermissions: ["process:host-user"] },
    },
  } }) } as Response);
  const props = {
    agentId: "late-provider", onSaved: vi.fn(),
    catalog: [{ resourceId: "model-a", kind: "model", name: "model-a", displayName: "Model A", version: "1", status: "ready" }],
  };
  const view = render(<AgentEditor {...props} providers={[]} />);
  const prompt = await screen.findByDisplayValue("Original instructions.");
  fireEvent.change(prompt, { target: { value: "Unsaved edited instructions." } });
  view.rerender(<AgentEditor {...props} providers={[provider]} />);
  const consent = await screen.findByRole("checkbox", { name: /确认 Codex Provider/ });
  expect(consent).toBeChecked();
  expect(screen.getByDisplayValue("Unsaved edited instructions.")).toBeVisible();
  expect(mockedFetch.mock.calls.filter(([path]) => path === "/api/v1/agents/late-provider")).toHaveLength(1);
  expect(mockedFetch.mock.calls.some(([, init]) => init?.method === "PUT")).toBe(false);
  // A manual rejection survives a same-content catalog refresh.
  fireEvent.click(consent);
  view.rerender(<AgentEditor {...props} providers={[{ ...provider }]} />);
  expect(screen.getByRole("checkbox", { name: /确认 Codex Provider/ })).not.toBeChecked();
  fireEvent.click(screen.getByRole("checkbox", { name: /确认 Codex Provider/ }));
  // New permissions on the same reference are not covered by the old click.
  view.rerender(<AgentEditor {...props} providers={[{ ...provider, permissions: ["process:host-user", "network:private"] }]} />);
  expect(screen.getByRole("checkbox", { name: /确认 Codex Provider/ })).not.toBeChecked();
  fireEvent.click(screen.getByRole("button", { name: "保存修改" }));
  expect(await screen.findByText("请先确认 Codex Provider 请求的 Agent 权限")).toBeVisible();
  expect(mockedFetch.mock.calls.filter(([path]) => path === "/api/v1/agents/late-provider")).toHaveLength(1);
  expect(mockedFetch.mock.calls.some(([, init]) => init?.method === "PUT")).toBe(false);
});

it("does not transfer an explicit confirmation to a different Provider", async () => {
  mockedFetch.mockReset();
  const first = {
    providerRef: "plugin://io.example.first@1.0.0", pluginId: "io.example.first",
    resolvedVersion: "1.0.0", displayName: "First", state: "enabled" as const,
    compatible: true, selectable: true, permissions: ["process:host-user"], isolation: "sidecar",
    configSchemaDeclared: false, secretFields: [],
  };
  const second = { ...first, providerRef: "plugin://io.example.second@1.0.0", pluginId: "io.example.second", displayName: "Second", permissions: ["network:private"] };
  mockedFetch.mockResolvedValue({ ok: true, json: async () => ({ draft: {
    metadata: { id: "switch-provider", name: "Switch", revision: 1 },
    spec: {
      runtime: { type: "plugin", providerRef: first.providerRef }, instructions: { system: "Answer the user." },
      bindings: { modelProfileId: "model-a", modelProfileIds: ["model-a"] }, security: { allowedPermissions: [] },
    },
  } }) } as Response);
  render(<AgentEditor agentId="switch-provider" providers={[first, second]} onSaved={vi.fn()}
    catalog={[{ resourceId: "model-a", kind: "model", name: "model-a", displayName: "Model A", version: "1", status: "ready" }]} />);
  fireEvent.click(await screen.findByRole("checkbox", { name: /确认 Provider/ }));
  const user = userEvent.setup();
  await user.click(screen.getByRole("combobox", { name: "AgentProvider" }));
  await user.click(screen.getByRole("option", { name: /Second/ }));
  expect(screen.getByRole("checkbox", { name: /确认 Provider/ })).not.toBeChecked();
  fireEvent.click(screen.getByRole("button", { name: "保存修改" }));
  expect(await screen.findByText("请先确认 AgentProvider 请求的权限")).toBeVisible();
  expect(mockedFetch.mock.calls.some(([, init]) => init?.method === "PUT")).toBe(false);
});

it("saves Harness child agents with authoritative parent tool names and retains advanced declarations", async () => {
  mockedFetch.mockReset();
  const child = { name: "review_helper", instructions: "Review independently", tools: [], maxTotalTokens: 4000, outputSchema: { type: "object" } };
  const draft = { metadata: { id: "harness-team", name: "Harness Team", revision: 1 }, spec: {
    runtime: { type: "harness" }, instructions: { system: "Delegate and review the result." },
    bindings: { modelProfileId: "model-a", modelProfileIds: ["model-a"], tools: [{ resourceId: "tool-read", enabled: true }] },
    subAgents: [child],
  } };
  mockedFetch.mockImplementation(async (_input, init) => ({ ok: true, json: async () => init?.method === "PUT" ? { metadata: { ...draft.metadata, revision: 2 }, spec: JSON.parse(String(init.body)) } : { draft } } as Response));
  const onSaved = vi.fn();
  render(<AgentEditor agentId="harness-team" onSaved={onSaved} catalog={[
    { resourceId: "model-a", kind: "model", name: "model-a", displayName: "Model A", version: "1", status: "ready" },
    { resourceId: "tool-read", kind: "tool", name: "display-alias", displayName: "读取文件", version: "1", status: "ready", contract: { name: "read_file", executor: "builtin" } },
  ]} />);
  await screen.findByRole("button", { name: "保存修改" });
  fireEvent.click(screen.getByRole("button", { name: /能力绑定/ }));
  fireEvent.click(screen.getByText("子 Agent", { selector: "summary", exact: false }));
  fireEvent.click(screen.getByRole("checkbox", { name: "读取文件" }));
  fireEvent.click(screen.getByRole("checkbox", { name: /保存后/ }));
  fireEvent.click(screen.getByRole("button", { name: "保存修改" }));
  await waitFor(() => expect(onSaved).toHaveBeenCalled());
  const sent = mockedFetch.mock.calls.find(([, init]) => init?.method === "PUT")!;
  expect(JSON.parse(String(sent[1]!.body)).subAgents).toEqual([{ ...child, tools: ["read_file"] }]);
});

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
    const rebuild = await screen.findByRole("checkbox", { name: /保存后立即重新构建/ });
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
    fireEvent.click(screen.getByRole("checkbox", { name: /保存后立即重新构建/ }));
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
});

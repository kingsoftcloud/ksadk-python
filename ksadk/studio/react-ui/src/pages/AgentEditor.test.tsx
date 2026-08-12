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
});

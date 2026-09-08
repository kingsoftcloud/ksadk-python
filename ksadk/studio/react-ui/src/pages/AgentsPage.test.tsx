import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { apiFetch } from "../api";
import { AgentsPage } from "./AgentsPage";

vi.mock("../api", () => ({ apiFetch: vi.fn() }));

const mockedFetch = vi.mocked(apiFetch);

describe("AgentsPage appearance", () => {
  beforeEach(() => {
    mockedFetch.mockReset();
    mockedFetch.mockResolvedValue({ ok: true, json: async () => ({ items: [] }) } as Response);
  });

  it("renders the persisted Agent image through the shared accessible avatar", async () => {
    render(
      <AgentsPage
        agents={[{
          metadata: {
            id: "avatar-agent",
            name: "联网研究助手",
            revision: 2,
            labels: { "agentkit.ksyun.com/template": "research" },
            appearance: {
              icon: "sparkles",
              color: "#7c5cc4",
              imageUrl: "/api/v1/assets/agent-avatars/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.png",
            },
          },
        }]}
        runtimeReady
        workspaceName="studio-test"
        onCreate={vi.fn()}
        onDetail={vi.fn()}
        onChat={vi.fn()}
        onBuild={vi.fn()}
        onChanged={vi.fn()}
      />,
    );

    const avatar = screen.getByLabelText("联网研究助手头像");
    expect(avatar).toHaveClass("agent-avatar");
    expect(avatar.querySelector("img")).toHaveAttribute(
      "src",
      "/api/v1/assets/agent-avatars/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.png",
    );
  });

  it("labels Codex YAML records as declaration validation rather than code bundles", async () => {
    render(
      <AgentsPage
        agents={[{
          metadata: { id: "yaml-agent", name: "YAML Agent", revision: 1 },
          spec: { runtime: { type: "codex" } },
          builds: [{ id: "declaration-1", status: "SUCCEEDED" }],
        }]}
        runtimeReady
        workspaceName="studio-test"
        onCreate={vi.fn()}
        onDetail={vi.fn()}
        onChat={vi.fn()}
        onBuild={vi.fn()}
        onChanged={vi.fn()}
      />,
    );

    expect(screen.getByText("声明已校验")).toBeInTheDocument();
    expect(screen.getByText("最近校验 / 构建")).toBeInTheDocument();
  });
});

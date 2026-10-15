import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { apiFetch } from "../api";
import { SkillDiscoveryDrawer } from "./ResourcesPage";

vi.mock("../api", () => ({ apiFetch: vi.fn() }));

const mockedFetch = vi.mocked(apiFetch);

function response(payload: unknown): Response {
  return { ok: true, json: async () => payload, text: async () => JSON.stringify(payload) } as Response;
}

describe("SkillDiscoveryDrawer", () => {
  beforeEach(() => {
    mockedFetch.mockReset();
    mockedFetch.mockImplementation(async input => {
      if (String(input) === "/api/v1/agents?limit=200") return response({ items: [] });
      throw new Error(`unexpected request: ${String(input)}`);
    });
  });

  it("discovers public GitHub skills through the inspect-before-install endpoint", async () => {
    const user = userEvent.setup();
    const base = mockedFetch.getMockImplementation()!;
    mockedFetch.mockImplementation(async (input, init) => {
      if (String(input) === "/api/v1/catalog/skills:discover-github") {
        expect(JSON.parse(String(init?.body))).toEqual({
          sourceUrl: "https://github.com/obra/superpowers",
          ref: "main",
        });
        return response({ inspectionToken: "skd-1", candidates: [] });
      }
      return base(input, init);
    });
    render(<SkillDiscoveryDrawer onClose={vi.fn()} onCatalogChanged={vi.fn()} />);

    await user.selectOptions(screen.getByLabelText("发现来源"), "github");
    await user.type(screen.getByLabelText("GitHub 仓库地址"), "https://github.com/obra/superpowers");
    await user.click(screen.getByRole("button", { name: "检查候选" }));

    await waitFor(() => expect(mockedFetch).toHaveBeenCalledWith(
      "/api/v1/catalog/skills:discover-github",
      expect.objectContaining({ method: "POST" }),
    ));
    expect(screen.getByText("该仓库中没有发现可安装的 SKILL.md。")).toBeVisible();
  });

  it("can bind imported skills to an agent and submit a fresh build", async () => {
    const user = userEvent.setup();
    mockedFetch.mockImplementation(async (input, init) => {
      const path = String(input);
      if (path === "/api/v1/agents?limit=200") {
        return response({ items: [{ metadata: { id: "agent-a", name: "Agent A" } }] });
      }
      if (path === "/api/v1/catalog/skills:discover") {
        return response({
          inspectionToken: "skd-2",
          candidates: [{
            candidateId: "candidate-a",
            status: "ready",
            name: "brainstorming",
            displayName: "Brainstorming",
            path: "skills/brainstorming",
            version: "1.0.0",
            fileCount: 1,
            totalBytes: 128,
          }],
        });
      }
      if (path === "/api/v1/catalog/skills/discoveries/skd-2:commit") {
        return response({ resourceId: "skill:local:brainstorming@1.0.0" });
      }
      if (path === "/api/v1/agents/agent-a") {
        return response({
          draft: {
            metadata: { id: "agent-a", revision: 4 },
            spec: { bindings: { skills: [{ resourceId: "skill:local:existing@1.0.0" }] } },
          },
        });
      }
      if (path === "/api/v1/agents/agent-a/bindings") {
        expect(init?.headers).toMatchObject({ "If-Match": "4" });
        expect(JSON.parse(String(init?.body)).skills).toEqual([
          { resourceId: "skill:local:existing@1.0.0" },
          { resourceId: "skill:local:brainstorming@1.0.0", enabled: true },
        ]);
        return response({ metadata: { id: "agent-a", revision: 5 } });
      }
      if (path === "/api/v1/agents/agent-a/builds") {
        expect(JSON.parse(String(init?.body))).toEqual({ revision: 5, runEvaluation: false });
        return response({ id: "operation-a" });
      }
      if (path === "/api/v1/operations/operation-a") {
        return response({ status: "SUCCEEDED" });
      }
      throw new Error(`unexpected request: ${path}`);
    });
    render(<SkillDiscoveryDrawer onClose={vi.fn()} onCatalogChanged={vi.fn()} />);

    await user.selectOptions(await screen.findByLabelText("安装后绑定到 Agent（可选）"), "agent-a");
    await user.click(screen.getByRole("button", { name: "检查候选" }));
    await user.click(await screen.findByLabelText("选择 Brainstorming"));
    await user.click(screen.getByRole("button", { name: "导入所选 1 个" }));

    await waitFor(() => expect(mockedFetch).toHaveBeenCalledWith(
      "/api/v1/operations/operation-a",
    ));
  });

  it("reports an interrupted build as a partial success instead of waiting forever", async () => {
    const user = userEvent.setup();
    mockedFetch.mockImplementation(async (input) => {
      const path = String(input);
      if (path === "/api/v1/agents?limit=200") {
        return response({ items: [{ metadata: { id: "agent-a", name: "Agent A" } }] });
      }
      if (path === "/api/v1/catalog/skills:discover") {
        return response({
          inspectionToken: "skd-interrupted",
          candidates: [{
            candidateId: "candidate-a",
            status: "ready",
            name: "brainstorming",
            displayName: "Brainstorming",
            path: "skills/brainstorming",
            version: "1.0.0",
            fileCount: 1,
            totalBytes: 128,
          }],
        });
      }
      if (path === "/api/v1/catalog/skills/discoveries/skd-interrupted:commit") {
        return response({ resourceId: "skill:local:brainstorming@1.0.0" });
      }
      if (path === "/api/v1/agents/agent-a") {
        return response({
          draft: { metadata: { revision: 4 }, spec: { bindings: { skills: [] } } },
        });
      }
      if (path === "/api/v1/agents/agent-a/bindings") {
        return response({ metadata: { revision: 5 } });
      }
      if (path === "/api/v1/agents/agent-a/builds") return response({ id: "operation-a" });
      if (path === "/api/v1/operations/operation-a") {
        return response({ status: "INTERRUPTED", error: { message: "构建被中断" } });
      }
      throw new Error(`unexpected request: ${path}`);
    });
    render(<SkillDiscoveryDrawer onClose={vi.fn()} onCatalogChanged={vi.fn()} />);

    await user.selectOptions(await screen.findByLabelText("安装后绑定到 Agent（可选）"), "agent-a");
    await user.click(screen.getByRole("button", { name: "检查候选" }));
    await user.click(await screen.findByLabelText("选择 Brainstorming"));
    await user.click(screen.getByRole("button", { name: "导入所选 1 个" }));

    expect(await screen.findByText(
      "已有 1 个 Skill 安装到资源目录，但后续绑定或构建未完成：构建被中断",
    )).toBeVisible();
  });

  it("shows concrete remote-code risk and discards the inspection on close", async () => {
    const user = userEvent.setup();
    const onClose = vi.fn();
    mockedFetch.mockImplementation(async (input, init) => {
      const path = String(input);
      if (path === "/api/v1/agents?limit=200") return response({ items: [] });
      if (path === "/api/v1/catalog/skills:discover-github") {
        return response({
          inspectionToken: "skd-risk",
          candidates: [{
            candidateId: "candidate-risk",
            status: "ready",
            name: "remote-skill",
            displayName: "Remote Skill",
            path: "github:owner/repo@abc/skills/remote",
            version: "1.0.0",
            fileCount: 3,
            totalBytes: 512,
            risk: {
              requiresReview: true,
              containsScripts: true,
              scripts: ["scripts/install.py"],
              networkMarkers: ["https://"],
            },
          }],
        });
      }
      if (path === "/api/v1/catalog/skills/discoveries/skd-risk") {
        expect(init?.method).toBe("DELETE");
        return response(null);
      }
      throw new Error(`unexpected request: ${path}`);
    });
    render(<SkillDiscoveryDrawer onClose={onClose} onCatalogChanged={vi.fn()} />);

    await user.selectOptions(screen.getByLabelText("发现来源"), "github");
    await user.type(screen.getByLabelText("GitHub 仓库地址"), "https://github.com/owner/repo");
    await user.click(screen.getByRole("button", { name: "检查候选" }));

    expect(await screen.findByText("复核原因：包含可执行脚本（1 个）；说明中包含网络访问指令")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "取消" }));
    await waitFor(() => expect(mockedFetch).toHaveBeenCalledWith(
      "/api/v1/catalog/skills/discoveries/skd-risk",
      { method: "DELETE" },
    ));
    expect(onClose).toHaveBeenCalledOnce();
  });
});

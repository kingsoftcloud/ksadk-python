import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

const { apiFetch } = vi.hoisted(() => ({ apiFetch: vi.fn() }));
let deploymentItems: unknown[] = [];
let scheduleEnabled = true;
let agentSchedulerAvailable = true;

const ownTask = () => ({
  taskId: "task-daily",
  displayName: "每日报告",
  target: { agentId: "demo-agent", agentVersionRef: "build-1", sessionId: null },
  schedule: { kind: "cron", timezone: "Asia/Shanghai", expression: "0 9 * * *", misfirePolicy: "run_once" },
  command: { payload: { content: "生成昨日摘要" } },
  enabled: scheduleEnabled,
  continuity: "new_session",
  nextRunAt: "2026-08-29T01:00:00Z",
});

const otherTask = {
  taskId: "task-other",
  displayName: "其他 Agent 任务",
  target: { agentId: "other-agent", agentVersionRef: "build-2", sessionId: null },
  schedule: { kind: "cron", timezone: "Asia/Shanghai", expression: "0 10 * * *", misfirePolicy: "skip" },
  command: { payload: { content: "不应出现在当前 Agent 详情" } },
  enabled: true,
  continuity: "new_session",
};

const succeededOccurrence = {
  occurrenceId: "occ-success",
  taskId: "task-daily",
  target: { agentId: "demo-agent", agentVersionRef: "build-1" },
  scheduledFor: "2026-08-28T01:00:00Z",
  trigger: "schedule",
  state: "succeeded",
  attempt: 1,
  commandId: "cmd-1",
  sessionId: "session-1",
  runId: "run-1",
  acceptedAt: "2026-08-28T01:00:01Z",
  startedAt: "2026-08-28T01:00:02Z",
  completedAt: "2026-08-28T01:00:03Z",
};

apiFetch.mockImplementation(async (path: string, init?: RequestInit) => {
  if (path === "/api/v1/agents/demo-agent") {
    return new Response(JSON.stringify({
      draft: {
        metadata: { id: "demo-agent", name: "Demo Agent", revision: 1 },
        spec: { runtime: { type: "langgraph" }, bindings: {} },
      },
      builds: [{ id: "build-1", status: "SUCCEEDED", bundleDigest: "sha256:bundle" }],
    }));
  }
  if (path === "/api/v1/agents/demo-agent/schedules") {
    if (!agentSchedulerAvailable) return new Response(null, { status: 404 });
    return new Response(JSON.stringify({ items: [ownTask()], availability: { available: true, running: true, triggerActive: true, store: "sqlite" } }));
  }
  if (path === "/api/v1/schedules") {
    return new Response(JSON.stringify({ items: [ownTask(), otherTask], availability: { available: true, running: true, triggerActive: true, store: "sqlite" } }));
  }
  if (path === "/api/v1/schedule-occurrences?limit=200") {
    return new Response(JSON.stringify({ items: [succeededOccurrence] }));
  }
  if (path === "/api/v1/agents/demo-agent/schedules/task-daily/occurrences") {
    return new Response(JSON.stringify({ items: [succeededOccurrence] }));
  }
  if (path === "/api/v1/agents/demo-agent/schedules/task-daily" && init?.method === "PUT") {
    scheduleEnabled = Boolean(JSON.parse(String(init.body)).enabled);
    return new Response(JSON.stringify(ownTask()));
  }
  if (path === "/api/v1/agents/demo-agent/schedules/task-daily:run" && init?.method === "POST") {
    return new Response(JSON.stringify({ occurrenceId: "occ-manual", state: "accepted" }));
  }
  if (path === "/api/v1/catalog/resources?limit=200") return new Response(JSON.stringify({ items: [] }));
  if (path === "/api/v1/deployments") return new Response(JSON.stringify({ items: deploymentItems }));
  throw new Error(path);
});

vi.mock("../api", () => ({ apiFetch }));

import { AgentDetailPage } from "./AgentDetailPage";

describe("AgentDetailPage cloud deployment", () => {
  beforeEach(() => {
    deploymentItems = [];
    scheduleEnabled = true;
    agentSchedulerAvailable = true;
    apiFetch.mockClear();
  });

  it("keeps legacy Agent details usable when no Agent scheduler endpoint is available", async () => {
    agentSchedulerAvailable = false;
    render(
      <AgentDetailPage
        agentId="demo-agent"
        onBack={vi.fn()}
        onChat={vi.fn()}
        onBuild={vi.fn()}
        onEdit={vi.fn()}
        onChanged={vi.fn()}
      />,
    );

    expect(await screen.findByText("角色与任务")).toBeInTheDocument();
    expect(await screen.findByText("还没有定时任务")).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "自动化" })).toBeInTheDocument();
  });

  it("opens the unified deployment flow for the exact successful Build", async () => {
    deploymentItems = [];
    window.location.hash = "#/agents/demo-agent";
    render(
      <AgentDetailPage
        agentId="demo-agent"
        onBack={vi.fn()}
        onChat={vi.fn()}
        onBuild={vi.fn()}
        onEdit={vi.fn()}
        onChanged={vi.fn()}
      />,
    );

    fireEvent.click(await screen.findByRole("button", { name: "部署到云端" }));

    expect(window.location.hash).toBe("#/deployments/new?buildId=build-1&agentId=demo-agent");
    expect(apiFetch).not.toHaveBeenCalledWith(
      "/api/v1/builds/build-1/deployments",
      expect.anything(),
    );
    expect(screen.queryByText(/preproduction|不上传代码包/)).not.toBeInTheDocument();
  });

  it("manages only this Agent's tasks and terminal history inside the automation tab", async () => {
    const user = userEvent.setup();
    render(
      <AgentDetailPage
        agentId="demo-agent"
        onBack={vi.fn()}
        onChat={vi.fn()}
        onBuild={vi.fn()}
        onEdit={vi.fn()}
        onChanged={vi.fn()}
      />,
    );

    await user.click(await screen.findByRole("tab", { name: "自动化" }));
    expect(await screen.findByText("该 Agent 的自动化")).toBeInTheDocument();
    expect(await screen.findByText("每日报告")).toBeInTheDocument();
    expect(screen.queryByText("其他 Agent 任务")).not.toBeInTheDocument();

    await user.click(screen.getByRole("row", { name: "查看定时任务 每日报告 的详情" }));
    expect((await screen.findAllByText("成功")).length).toBeGreaterThan(0);
    expect(screen.getByText("run-1")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "停用" }));
    await waitFor(() => expect(scheduleEnabled).toBe(false));
    expect(apiFetch).toHaveBeenCalledWith(
      "/api/v1/agents/demo-agent/schedules/task-daily",
      expect.objectContaining({ method: "PUT" }),
    );

    await user.click(screen.getByRole("button", { name: "立即运行" }));
    await waitFor(() => expect(apiFetch).toHaveBeenCalledWith(
      "/api/v1/agents/demo-agent/schedules/task-daily:run",
      { method: "POST" },
    ));
  });

  it("shows a ready cloud receipt instead of offering a duplicate deployment", async () => {
    deploymentItems = [{
      id: "dep-1", buildId: "build-1", agentId: "ar-cloud-1", status: "READY",
    }];

    render(
      <AgentDetailPage
        agentId="demo-agent"
        onBack={vi.fn()}
        onChat={vi.fn()}
        onBuild={vi.fn()}
        onEdit={vi.fn()}
        onChanged={vi.fn()}
      />,
    );

    expect(await screen.findByText("云端实例运行中")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "查看云端部署" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "查看云端部署" }));
    await waitFor(() => expect(window.location.hash).toBe("#/deployments/dep-1"));
  });

  it("keeps edit, build, and deployment actions available from the compact overflow menu", async () => {
    deploymentItems = [];
    const user = userEvent.setup();
    render(
      <AgentDetailPage
        agentId="demo-agent"
        onBack={vi.fn()}
        onChat={vi.fn()}
        onBuild={vi.fn()}
        onEdit={vi.fn()}
        onChanged={vi.fn()}
      />,
    );

    await user.click(await screen.findByRole("button", { name: "Demo Agent 的更多操作" }));
    expect(await screen.findByRole("menuitem", { name: "编辑" })).toBeInTheDocument();
    expect(screen.getByRole("menuitem", { name: "校验并构建" })).toBeInTheDocument();
    expect(screen.getByRole("menuitem", { name: "部署到云端" })).toBeInTheDocument();
  });
});

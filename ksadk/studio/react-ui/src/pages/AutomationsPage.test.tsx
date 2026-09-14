import { render, screen, within, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, it, vi } from "vitest";
const { apiFetch } = vi.hoisted(() => ({ apiFetch: vi.fn() }));
vi.mock("../api", () => ({ apiFetch }));
import { AutomationsPage } from "./AutomationsPage";

const agents = [{ metadata: { id: "agent-1", name: "工作助手" } }];
const task = { taskId: "task-1", displayName: "每日报告", target: { agentId: "agent-1" }, command: { payload: { content: "总结进展" } }, schedule: { kind: "cron", expression: "0 10 * * *", timezone: "Asia/Shanghai" }, enabled: true, continuity: "new_session" };
let items: typeof task[];
beforeEach(() => {
  items = [task];
  apiFetch.mockReset();
  apiFetch.mockImplementation(async (path, init) => {
    if (path === "/api/v1/schedules") return Response.json({ items, availability: { available: true, triggerActive: true } });
    if (path === "/api/v1/schedule-occurrences?limit=200") return Response.json({ items: [{ taskId: "task-1", state: "failed", target: { agentId: "agent-1" } }] });
    if (path.endsWith("/occurrences")) return Response.json({ items: [] });
    if (path === "/api/v1/agents/agent-1/schedules" && init?.method === "POST") {
      const body = JSON.parse(init.body);
      items = [...items, { ...task, ...body, taskId: "task-2" }];
      return Response.json(items[1]);
    }
    throw new Error(path);
  });
});
const renderPage = () => render(<AutomationsPage currentAgentId="agent-1" agents={agents} onSelectAgent={vi.fn()} />);

it("groups failures on the board, searches, and switches to the list", async () => {
  const user = userEvent.setup(); renderPage();
  expect(await within(screen.getByRole("region", { name: "需关注" })).findByRole("button", { name: "查看定时任务 每日报告 的详情" })).toBeInTheDocument();
  await user.type(screen.getByRole("textbox", { name: "搜索任务" }), "不存在");
  expect(screen.queryByRole("button", { name: "查看定时任务 每日报告 的详情" })).not.toBeInTheDocument();
  await user.clear(screen.getByRole("textbox", { name: "搜索任务" }));
  await user.click(screen.getByRole("button", { name: "列表视图" }));
  expect(screen.getByRole("row", { name: "查看定时任务 每日报告 的详情" })).toBeInTheDocument();
});

it("creates an interval from a template without requiring cron or seconds", async () => {
  const user = userEvent.setup(); renderPage();
  await user.click(screen.getByRole("button", { name: /定期巡检/ }));
  const dialog = screen.getByRole("dialog");
  await user.clear(within(dialog).getByRole("spinbutton", { name: "每隔" }));
  await user.type(within(dialog).getByRole("spinbutton", { name: "每隔" }), "7");
  await user.click(within(dialog).getByRole("button", { name: "创建任务" }));
  await waitFor(() => expect(apiFetch).toHaveBeenCalledWith("/api/v1/agents/agent-1/schedules", expect.objectContaining({ method: "POST" })));
  const body = JSON.parse(apiFetch.mock.calls.find(([, init]) => init?.method === "POST")![1].body);
  expect(body.schedule.everySeconds).toBe(420);
  expect(body.prompt).toContain("检查当前服务状态");
  expect(body.schedule.misfirePolicy).toBe("skip");
});

it("keeps a failed save visible and preserves the draft", async () => {
  const user = userEvent.setup(); renderPage();
  await user.click(screen.getByRole("button", { name: /每日简报/ }));
  apiFetch.mockImplementation(async () => Response.json({ error: { message: "当前模型未配置" } }, { status: 422 }));
  await user.click(within(screen.getByRole("dialog")).getByRole("button", { name: "创建任务" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("当前模型未配置");
  expect(screen.getByRole("textbox", { name: "任务名称" })).toHaveValue("每日工作简报");
});

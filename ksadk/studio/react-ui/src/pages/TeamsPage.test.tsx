import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { TeamsPage } from "./TeamsPage";
import { teamSnapshot, memberRef, teamExecution } from "../test/teamsFixtures";
import {
  type GroupSnapshot,
  TEAMS_API_VERSION,
} from "@kingsoftcloud/ksadk-web/teams";

const { fetchMock } = vi.hoisted(() => ({ fetchMock: vi.fn() }));
vi.mock("../api", () => ({ apiFetch: fetchMock }));
let snapshot: GroupSnapshot;
const requests: { url: string; init: RequestInit }[] = [];
const streams: { url: string; signal?: AbortSignal | null }[] = [];
const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });

beforeEach(() => {
  snapshot = teamSnapshot();
  requests.length = 0;
  streams.length = 0;
  window.history.replaceState(
    null,
    "",
    "#/workspace/teams?groupId=fixture-group",
  );
  HTMLDialogElement.prototype.showModal = function () {
    this.setAttribute("open", "");
  };
  HTMLDialogElement.prototype.close = function () {
    this.removeAttribute("open");
  };
  fetchMock
    .mockReset()
    .mockImplementation(async (url: string, init: RequestInit = {}) => {
      requests.push({ url, init });
      if (url.endsWith("/lifecycle"))
        return json({
          enabled: true,
          apiVersion: TEAMS_API_VERSION,
          health: "ready",
          authorityRef: "fixture-local",
        });
      if (url.includes("/members/") && url.includes("conversation?"))
        return json({ ref: memberRef(), cursor: 0, items: [] });
      if (url.includes("/events?")) {
        streams.push({ url, signal: init.signal });
        return new Response(
          new ReadableStream({
            start(controller) {
              init.signal?.addEventListener("abort", () => {
                try {
                  controller.close();
                } catch {}
              });
            },
          }),
          { headers: { "Content-Type": "text/event-stream" } },
        );
      }
      if (url.startsWith("/api/v1/groups?") || url === "/api/v1/groups")
        return json({
          items: [
            {
              ...snapshot.group,
              memberCount: 2,
              pendingCount: snapshot.interactions.length,
              unreadCount: 0,
            },
          ],
        });
      if (url.endsWith("/bindings"))
        return json({
          items: snapshot.members.map((member) => ({
            ...member.binding,
            name: member.name,
          })),
        });
      if (init.method === "PATCH") return json(snapshot);
      if (url.includes("/execution?")) return json(teamExecution(snapshot));
      if (url.endsWith("/read")) return new Response(null, { status: 204 });
      if (url.endsWith("/cancel")) return json({ status: "cancel_requested", runId: memberRef().runId }, 202);
      if (init.method === "POST")
        return json({
          status: "accepted",
          groupId: snapshot.group.groupId,
          messageId: "accepted-message",
          teamRunId: "team-run",
        });
      return json(snapshot);
    });
});
afterEach(() => vi.restoreAllMocks());

describe("Studio Teams API integration", () => {
  it("does not fall back to local snapshots when cloud projection support is missing", async () => {
    fetchMock.mockResolvedValue(json({ enabled: true, apiVersion: TEAMS_API_VERSION, health: "degraded", mode: "server", authorityRef: "fixture-local", authorityLocation: "server", failure: { code: "local_node_unavailable", reason: "本地节点暂时不可用" } }));
    render(<TeamsPage />);
    expect(await screen.findByRole("heading", { name: "云端团队正在准备" })).toBeInTheDocument();
    expect(screen.queryByRole("textbox", { name: "给团队的消息" })).not.toBeInTheDocument();
    expect(fetchMock.mock.calls.every(([url]) => String(url).endsWith("/lifecycle"))).toBe(true);
  });

  it("requires original terminal evidence and preserves reconciliation input after a conflict", async () => {
    snapshot.teamRuns[0].leaderStandby = { groupId: snapshot.group.groupId, teamRunId: "team-run", state: "active", primaryNodeId: "local", primarySessionId: "prior-session", primaryBindingRef: "binding-leader", standbyNodeId: "cloud", standbyBindingRef: "cloud-leader", buildDigest: "digest", epoch: 2, reason: "execution_uncertain_fenced", automaticFailback: false };
    const original = fetchMock.getMockImplementation()!;
    const records = [
      { commandId: "unknown-order", teamRunId: "team-run", nodeId: "local", state: "fenced", reconciled: false, receiptDigest: null },
      { commandId: "ended-order", teamRunId: "team-run", nodeId: "local", state: "fenced", reconciled: false, receiptDigest: "sha256:" + "a".repeat(64), quarantinedReceipt: { run_id: "original-run", run_status: "succeeded", output: "已保存原执行成果" } },
    ];
    let attempts = 0;
    fetchMock.mockImplementation(async (url: string, init: RequestInit = {}) => {
      if (url.endsWith("/reconciliation")) return json({ groupId: snapshot.group.groupId, items: records });
      if (url.endsWith("/reconciliation/ended-order")) {
        requests.push({ url, init });
        if (++attempts === 1) return json({ error: { code: "reconciliation_receipt_changed", message: "原单证据已更新，请刷新后核对" } }, 409);
        records[1].reconciled = true;
        return json({ status: "reconciled", commandId: "ended-order", teamRunId: "team-run" });
      }
      return original(url, init);
    });
    render(<TeamsPage />);
    await screen.findByRole("heading", { name: snapshot.group.name });
    await userEvent.click(screen.getByRole("button", { name: "查看任务详情" }));
    await userEvent.click(await screen.findByRole("button", { name: "查看原单证据" }));
    expect(await screen.findByText(/原节点尚未返回结束回执/)).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: "确认已结束" })).toHaveLength(1);
    expect(screen.getByRole("button", { name: "确认已结束" })).toBeDisabled();
    await userEvent.type(screen.getByRole("textbox", { name: "核查说明" }), "已核对原单与产物");
    await userEvent.click(screen.getByRole("button", { name: "确认已结束" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("原单证据已更新");
    expect(screen.getByRole("textbox", { name: "核查说明" })).toHaveValue("已核对原单与产物");
    await userEvent.click(screen.getByRole("button", { name: "确认已结束" }));
    await waitFor(() => expect(screen.queryByRole("textbox", { name: "核查说明" })).not.toBeInTheDocument());
    const sent = requests.filter(row => row.url.endsWith("/reconciliation/ended-order"));
    expect(sent).toHaveLength(2);
    expect(JSON.parse(String(sent[0].init.body))).toMatchObject({ action: "confirm_ended", receiptDigest: "sha256:" + "a".repeat(64), reason: "已核对原单与产物" });
    expect(JSON.parse(String(sent[0].init.body)).idempotencyKey).toBe(JSON.parse(String(sent[1].init.body)).idempotencyKey);
  });

  it("loads a real group snapshot, starts only group observation and keeps graphs opt-in", async () => {
    const { unmount } = render(<TeamsPage />);
    expect(
      await screen.findByRole("heading", { name: snapshot.group.name }),
    ).toBeInTheDocument();
    await waitFor(() => expect(streams.length).toBe(1));
    expect(streams[0].url).toBe("/api/v1/groups/fixture-group/events?after=0");
    expect(
      screen.queryByRole("region", { name: "执行工作台" }),
    ).not.toBeInTheDocument();
    expect(requests.filter((row) => row.init.method === "POST")).toHaveLength(
      0,
    );
    unmount();
    expect(streams[0].signal?.aborted).toBe(true);
  });

  it("observes existing member Run with GET and closes the subscription without stopping it", async () => {
    render(<TeamsPage />);
    await screen.findByRole("heading", { name: snapshot.group.name });
    await userEvent.click(screen.getByRole("button", { name: /^2 位成员/ }));
    await userEvent.click(screen.getByRole("button", { name: /工程师/ }));
    await screen.findByText("执行详情只读。关闭面板不会停止成员。");
    await waitFor(() => expect(streams).toHaveLength(2));
    const observed = streams[1];
    expect(observed.url).toContain(
      "sessionId=session-engineer&runId=run-engineer&bindingRef=binding-engineer",
    );
    await userEvent.click(screen.getByRole("button", { name: "在群内 @成员" }));
    await waitFor(() => expect(observed.signal?.aborted).toBe(true));
    expect(screen.getByRole("combobox", { name: "接收成员" })).toHaveValue(
      "engineer",
    );
    expect(screen.getByRole("textbox", { name: "给团队的消息" })).toHaveFocus();
    expect(requests.some((row) => row.url.endsWith("/control"))).toBe(false);
  });

  it("requests cancellation of only the observed current Run and waits for its real terminal event", async () => {
    render(<TeamsPage />);
    await screen.findByRole("heading", { name: snapshot.group.name });
    await userEvent.click(screen.getByRole("button", { name: /^2 位成员/ }));
    await userEvent.click(screen.getByRole("button", { name: /工程师/ }));
    await userEvent.click(await screen.findByRole("button", { name: "请求停止此执行" }));
    const sent = requests.find(row => row.url.endsWith("/cancel"))!;
    expect(sent.url).toBe("/api/v1/groups/fixture-group/members/engineer/cancel");
    expect(JSON.parse(String(sent.init.body))).toMatchObject({ ref: memberRef(), idempotencyKey: expect.any(String) });
    expect(await screen.findByText(/已请求停止，等待执行端确认/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "已请求停止" })).toBeDisabled();
    expect(screen.getAllByText(/进行中/).length).toBeGreaterThan(0);
    expect(requests.some(row => row.url.endsWith("/control"))).toBe(false);
  });

  it("reuses the cancellation idempotency key after an uncertain request failure", async () => {
    const original = fetchMock.getMockImplementation()!;
    let attempts = 0;
    const keys: string[] = [];
    fetchMock.mockImplementation(async (url: string, init: RequestInit = {}) => {
      if (url.endsWith("/cancel")) {
        keys.push(JSON.parse(String(init.body)).idempotencyKey);
        if (++attempts === 1) throw new Error("网络中断，请重试");
      }
      return original(url, init);
    });
    render(<TeamsPage />);
    await screen.findByRole("heading", { name: snapshot.group.name });
    await userEvent.click(screen.getByRole("button", { name: /^2 位成员/ }));
    await userEvent.click(screen.getByRole("button", { name: /工程师/ }));
    await userEvent.click(await screen.findByRole("button", { name: "请求停止此执行" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("网络中断");
    await userEvent.click(screen.getByRole("button", { name: "请求停止此执行" }));
    await screen.findByText(/已请求停止，等待执行端确认/);
    expect(keys).toHaveLength(2);
    expect(new Set(keys).size).toBe(1);
  });

  it("does not offer cancellation for an observed historical Run", async () => {
    const historical = { ...memberRef(), bindingRef: "prior-binding", sessionId: "prior-session", runId: "prior-run" };
    snapshot.messages[0].sourceRefs = [historical];
    render(<TeamsPage />);
    await screen.findByRole("heading", { name: snapshot.group.name });
    await userEvent.click(screen.getByRole("button", { name: /^2 位成员/ }));
    await userEvent.click(screen.getByRole("button", { name: /工程师/ }));
    const selector = screen.getByRole("combobox", { name: "成员执行记录" });
    const historicalOption = screen.getByRole("option", { name: /历史执行.*prior-run/ });
    await userEvent.selectOptions(selector, historicalOption);
    expect(screen.queryByRole("button", { name: "请求停止此执行" })).not.toBeInTheDocument();
  });

  it("sends directed input through group domain and never opens a second execution", async () => {
    render(<TeamsPage />);
    await screen.findByRole("heading", { name: snapshot.group.name });
    await userEvent.selectOptions(
      screen.getByRole("combobox", { name: "接收成员" }),
      "engineer",
    );
    await userEvent.type(
      screen.getByRole("textbox", { name: "给团队的消息" }),
      "请核对异常场景",
    );
    await userEvent.click(screen.getByRole("button", { name: "发送" }));
    await waitFor(() =>
      expect(requests.some((row) => row.url.endsWith("/messages"))).toBe(true),
    );
    const sent = requests.find((row) => row.url.endsWith("/messages"))!;
    expect(JSON.parse(String(sent.init.body))).toMatchObject({
      intent: "directed",
      mentions: ["engineer"],
      parts: [{ kind: "text", text: "请核对异常场景" }],
    });
    expect(
      requests.some(
        (row) =>
          /stream|agent-control|team-runs$/.test(row.url) &&
          row.init.method === "POST",
      ),
    ).toBe(false);
  });

  it("submits duplicate-named interactions using the full member reference and waits for confirmation", async () => {
    snapshot.interactions = ["leader", "engineer"].map((member) => ({
      ref: { ...memberRef(member), interactionId: "same-name" },
      revision: 2,
      title: `确认 ${member}`,
      message: "执行操作前确认",
      kind: "approval",
      status: "pending",
      createdAt: snapshot.group.createdAt,
    }));
    render(<TeamsPage />);
    await screen.findByRole("heading", { name: snapshot.group.name });
    await userEvent.click(screen.getByRole("button", { name: /项需要处理/ }));
    await userEvent.click(screen.getAllByRole("button", { name: "同意" })[1]);
    const sent = requests.find((row) => row.url.endsWith("/interactions"))!;
    expect(JSON.parse(String(sent.init.body))).toMatchObject({
      ref: { ...memberRef(), interactionId: "same-name" },
      expectedRevision: 2,
      action: "approve",
    });
    expect(
      await screen.findByText("已提交，等待执行端确认"),
    ).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: "同意" })).toHaveLength(1);
  });
  it("renames an active team without accidentally sending structural changes", async () => {
    render(<TeamsPage />);
    await screen.findByRole("heading", { name: snapshot.group.name });
    await userEvent.click(screen.getByRole("button", { name: "设置" }));
    expect(screen.getByRole("combobox", { name: "Leader" })).toBeEnabled();
    const name = screen.getByRole("textbox", { name: "群组名称" });
    await userEvent.clear(name);
    await userEvent.type(name, "新的团队名称");
    await userEvent.click(screen.getByRole("button", { name: "保存设置" }));
    await waitFor(() =>
      expect(requests.some((row) => row.init.method === "PATCH")).toBe(true),
    );
    const patch = JSON.parse(
      String(requests.find((row) => row.init.method === "PATCH")!.init.body),
    );
    expect(patch).toMatchObject({ name: "新的团队名称", expectedRevision: 1 });
    expect(patch).not.toHaveProperty("leaderMemberId");
    expect(patch).not.toHaveProperty("taskAcceptance");
  });

  it("adds members using only a selected server binding and a fresh member identity", async () => {
    snapshot.teamRuns[0].status = "succeeded";
    render(<TeamsPage />);
    await screen.findByRole("heading", { name: snapshot.group.name });
    await userEvent.click(screen.getByRole("button", { name: "设置" }));
    await userEvent.selectOptions(
      screen.getByRole("combobox", { name: "成员变更" }),
      "add",
    );
    await userEvent.selectOptions(
      screen.getByRole("combobox", { name: "可用 Agent" }),
      "binding-engineer",
    );
    await userEvent.click(screen.getByRole("button", { name: "保存设置" }));
    await waitFor(() =>
      expect(requests.some((row) => row.init.method === "PATCH")).toBe(true),
    );
    const patch = JSON.parse(
      String(requests.find((row) => row.init.method === "PATCH")!.init.body),
    );
    expect(patch.addMember).toEqual({
      memberId: expect.stringMatching(/^member-/),
      name: "工程师",
      bindingRef: "binding-engineer",
    });
    expect(patch).toMatchObject({
      expectedRevision: 1,
      idempotencyKey: expect.any(String),
    });
  });
  it("references a selected artifact through group message parts without uploading a local path", async () => {
    snapshot.tasks[1].attempts[0].artifacts = [
      {
        artifactId: "artifact-owned",
        name: "review.md",
        mediaType: "text/markdown",
        source: memberRef(),
        uri: "/api/v1/groups/fixture-group/artifacts/artifact-owned/download",
      },
    ];
    render(<TeamsPage />);
    await screen.findByRole("heading", { name: snapshot.group.name });
    await userEvent.selectOptions(
      screen.getByRole("combobox", { name: "引用本群交付物" }),
      "artifact-owned",
    );
    await userEvent.type(
      screen.getByRole("textbox", { name: "给团队的消息" }),
      "请继续检查这份交付物",
    );
    await userEvent.click(screen.getByRole("button", { name: "发送" }));
    await waitFor(() =>
      expect(requests.some((row) => row.url.endsWith("/messages"))).toBe(true),
    );
    const body = JSON.parse(
      String(requests.find((row) => row.url.endsWith("/messages"))!.init.body),
    );
    expect(body.parts).toEqual([
      { kind: "text", text: "请继续检查这份交付物" },
      {
        kind: "attachment",
        attachmentRef: "artifact-owned",
        mediaType: "text/markdown",
        name: "review.md",
      },
    ]);
    expect(body.parts[1]).not.toHaveProperty("uri");
    expect(
      screen.queryByRole("button", { name: "移除引用 review.md" }),
    ).not.toBeInTheDocument();
  });
});

describe("independent team tasks and recovery", () => {
  function withTwoTasks() {
    const first = snapshot.teamRuns[0];
    snapshot.teamRuns.push({ ...first, teamRunId: "second-run", goal: "独立的第二个目标", goalMessageId: "second-goal" });
    snapshot.messages.forEach(message => { message.teamRunId = first.teamRunId; });
    snapshot.messages.push({ ...snapshot.messages[0], messageId: "second-goal", teamRunId: "second-run", parts: [{ kind: "text", text: "仅属于第二个任务的内容" }] });
    snapshot.runMembers = snapshot.teamRuns.flatMap(run => snapshot.members.map(member => ({ ...member, runMemberId: `${run.teamRunId}-${member.memberId}`, teamRunId: run.teamRunId, groupRevision: 1, sessionId: `${run.teamRunId}-${member.sessionId}` })));
  }
  it("keeps conversation, drafts and outgoing target within the selected task", async () => {
    withTwoTasks();
    render(<TeamsPage />);
    await screen.findByRole("heading", { name: snapshot.group.name });
    expect(screen.queryByText("仅属于第二个任务的内容")).not.toBeInTheDocument();
    const input = screen.getByRole("textbox", { name: "给团队的消息" });
    await userEvent.type(input, "第一份草稿");
    await userEvent.click(screen.getByRole("button", { name: /独立的第二个目标/ }));
    expect(input).toHaveValue("");
    expect(screen.getByText("仅属于第二个任务的内容")).toBeInTheDocument();
    await userEvent.type(input, "第二份草稿");
    await userEvent.click(screen.getByRole("button", { name: /^输出接口改造方案与验证结果/ }));
    expect(input).toHaveValue("第一份草稿");
    await userEvent.click(screen.getByRole("button", { name: "发送" }));
    const sent = requests.find(row => row.url.endsWith("/messages"))!;
    expect(JSON.parse(String(sent.init.body))).toMatchObject({ teamRunId: "team-run", intent: "followup", parts: [{ kind: "text", text: "第一份草稿" }] });
  });
  it("starts an explicitly new task with an @recipient while other tasks run", async () => {
    withTwoTasks();
    render(<TeamsPage />);
    await screen.findByRole("heading", { name: snapshot.group.name });
    await userEvent.click(screen.getByRole("button", { name: /新任务/ }));
    await userEvent.selectOptions(screen.getByRole("combobox", { name: "接收成员" }), "engineer");
    await userEvent.type(screen.getByRole("textbox", { name: "给团队的消息" }), "新的独立目标");
    await userEvent.click(screen.getByRole("button", { name: "发送" }));
    const body = JSON.parse(String(requests.find(row => row.url.endsWith("/messages"))!.init.body));
    expect(body).toMatchObject({ intent: "start_goal", mentions: ["engineer"] });
    expect(body).not.toHaveProperty("teamRunId");
  });
  it("sends a reasoned request for changes to the same task", async () => {
    snapshot.teamRuns[0].status = "awaiting_acceptance";
    render(<TeamsPage />);
    await screen.findByRole("heading", { name: snapshot.group.name });
    await userEvent.click(screen.getByRole("button", { name: "提出修改" }));
    expect(screen.getByRole("button", { name: "提交修改意见" })).toBeDisabled();
    await userEvent.type(screen.getByRole("textbox", { name: "修改意见" }), "请补充失败恢复场景");
    await userEvent.click(screen.getByRole("button", { name: "提交修改意见" }));
    const request = requests.find(row => row.url.endsWith("/acceptance"))!;
    expect(request.url).toContain("/team-runs/team-run/");
    expect(JSON.parse(String(request.init.body))).toMatchObject({ action: "request_changes", reason: "请补充失败恢复场景" });
    expect(JSON.parse(String(request.init.body))).not.toHaveProperty("accepted");
  });
  it("preserves a failed creation draft and member identities across close and refresh", async () => {
    const original = fetchMock.getMockImplementation()!;
    const creations: Record<string, unknown>[] = [];
    fetchMock.mockImplementation(async (url: string, init: RequestInit = {}) => {
      if (url === "/api/v1/groups" && init.method === "POST") { creations.push(JSON.parse(String(init.body))); throw new Error("回执暂未确认"); }
      return original(url, init);
    });
    render(<TeamsPage />);
    await screen.findByRole("heading", { name: snapshot.group.name });
    await userEvent.click(screen.getByRole("button", { name: "创建团队" }));
    await userEvent.click(await screen.findByRole("checkbox", { name: /协调助手/ }));
    await userEvent.click(screen.getByRole("checkbox", { name: /工程师/ }));
    await userEvent.type(screen.getByRole("textbox", { name: "团队名称" }), "重试验证团队");
    await userEvent.click(within(screen.getByRole("dialog", { name: "组建团队" })).getByRole("button", { name: "创建团队" }));
    await screen.findByText("回执暂未确认");
    await userEvent.click(screen.getByRole("button", { name: "稍后继续" }));
    await userEvent.click(screen.getByRole("button", { name: "创建团队" }));
    await userEvent.click(screen.getByRole("button", { name: "刷新" }));
    await waitFor(() => expect(within(screen.getByRole("dialog", { name: "组建团队" })).getByRole("button", { name: "创建团队" })).toBeEnabled());
    expect(screen.getByRole("textbox", { name: "团队名称" })).toHaveValue("重试验证团队");
    await userEvent.click(within(screen.getByRole("dialog", { name: "组建团队" })).getByRole("button", { name: "创建团队" }));
    await waitFor(() => expect(creations).toHaveLength(2));
    expect(creations[1]).toEqual(creations[0]);
  });
  it("shows recovery for a configured plugin that failed before activation", async () => {
    const original = fetchMock.getMockImplementation()!;
    fetchMock.mockImplementation(async (url: string, init: RequestInit = {}) => {
      if (url.endsWith("/lifecycle")) return json({ enabled: false, configuredEnabled: true, apiVersion: TEAMS_API_VERSION, health: "failed", authorityRef: "fixture-local", failure: { code: "artifact_migration_required", stage: "teams.start", retryable: false, reason: "需要完成数据迁移" } });
      return original(url, init);
    });
    render(<TeamsPage />);
    expect(await screen.findByRole("heading", { name: "团队暂时无法启动" })).toBeInTheDocument();
    expect(screen.getByText("需要完成数据迁移")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "修复与诊断" })).toHaveAttribute("href", "/studio-recovery/");
    expect(screen.getByRole("button", { name: "暂时禁用" })).toBeEnabled();
    expect(requests.some(row => row.url.startsWith("/api/v1/groups"))).toBe(false);
  });
});

describe("task execution configuration", () => {
  it("does not expose legacy standby creation just because cloud lifecycle says ready", async () => {
    fetchMock.mockResolvedValue(json({ enabled: true, apiVersion: TEAMS_API_VERSION, health: "ready", mode: "server", authorityId: "cloud-authority", ownerScopeRef: "cloud-owner", features: [] }));
    render(<TeamsPage />);
    expect(await screen.findByRole("heading", { name: "云端团队正在准备" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "创建团队" })).not.toBeInTheDocument();
    expect(fetchMock.mock.calls.every(([url]) => String(url).endsWith("/lifecycle"))).toBe(true);
  });

  it("sends workspace options and only the explicitly selected cross-task artifact", async () => {
    snapshot.artifacts = [{ artifactId: "prior-report", name: "prior-review.md", mediaType: "text/markdown", source: memberRef() }];
    render(<TeamsPage />);
    await screen.findByRole("heading", { name: snapshot.group.name });
    await userEvent.click(screen.getByRole("button", { name: /新任务/ }));
    await userEvent.click(screen.getByLabelText("更多消息选项"));
    await userEvent.click(screen.getByText("工作目录 · 可选"));
    await userEvent.type(screen.getByRole("textbox", { name: "工作目录" }), "/node/workspace/project");
    await userEvent.type(screen.getByRole("textbox", { name: "Git 基线" }), "main");
    await userEvent.type(screen.getByRole("textbox", { name: "输入文件" }), "README.md\nsrc/main.py");
    await userEvent.selectOptions(screen.getByRole("combobox", { name: "引用本群交付物" }), "prior-report");
    await userEvent.type(screen.getByRole("textbox", { name: "给团队的消息" }), "请验证接口兼容性");
    await userEvent.click(screen.getByRole("button", { name: "发送" }));
    const body = JSON.parse(String(requests.find(row => row.url.endsWith("/messages"))!.init.body));
    expect(body.workspace).toEqual({ sourcePath: "/node/workspace/project", baseRef: "main", inputs: ["README.md", "src/main.py"], mode: "auto" });
    expect(body.intent).toBe("start_goal");
    expect(body.parts).toEqual([{ kind: "text", text: "请验证接口兼容性" }, { kind: "attachment", attachmentRef: "prior-report", mediaType: "text/markdown", name: "prior-review.md" }]);
  });
  it("renders the real final result before asking for acceptance", async () => {
    snapshot.teamRuns[0].status = "awaiting_acceptance";
    snapshot.teamRuns[0].result = "### 已完成的交付\n\n请求格式已验证，兼容性测试通过。";
    render(<TeamsPage />);
    expect(await screen.findByRole("heading", { name: "已完成的交付" })).toBeInTheDocument();
    expect(screen.getByText("请求格式已验证，兼容性测试通过。")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "本轮进展" })).not.toBeInTheDocument();
  });
  it("requires trusted owner scope even when workspace and write features are advertised", async () => {
    fetchMock.mockResolvedValue(json({ enabled: true, apiVersion: TEAMS_API_VERSION, health: "ready", mode: "server", authorityRef: "fixture-local", authorityLocation: "server", features: ["workspace-projection.v1", "durable-operations.v1"] }));
    render(<TeamsPage />);
    expect(await screen.findByRole("heading", { name: "云端团队正在准备" })).toBeInTheDocument();
    expect(screen.queryByText("云端接管设置")).not.toBeInTheDocument();
    expect(fetchMock.mock.calls.every(([url]) => String(url).endsWith("/lifecycle"))).toBe(true);
  });
});

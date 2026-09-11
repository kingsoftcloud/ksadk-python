import { act, render, screen, waitFor } from "@testing-library/react";
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
    expect(screen.getByRole("combobox", { name: "Leader" })).toBeDisabled();
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

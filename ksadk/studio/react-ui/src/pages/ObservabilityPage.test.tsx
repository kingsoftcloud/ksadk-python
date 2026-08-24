import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { apiFetch } from "../api";
import { showToast } from "../components/Toast";
import { ObservabilityPage } from "./ObservabilityPage";

vi.mock("../api", () => ({ apiFetch: vi.fn() }));
vi.mock("../components/Toast", () => ({ showToast: vi.fn() }));

class FakeEventSource {
  close() {}
  addEventListener() {}
}

function response(value: unknown): Promise<Response> {
  return Promise.resolve(new Response(JSON.stringify(value), { status: 200 }));
}

function downloadResponse(): Promise<Response> {
  return Promise.resolve(new Response('{"type":"session"}\n', {
    status: 200,
    headers: {
      "Content-Type": "application/x-ndjson",
      "X-Session-Event-Count": "3",
    },
  }));
}

describe("ObservabilityPage trajectory integration", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.stubGlobal("EventSource", FakeEventSource);
    vi.mocked(apiFetch).mockImplementation((input, init) => {
      const url = String(input);
      if (url.startsWith("/api/v1/agents")) return response({ items: [] });
      if (url.startsWith("/api/v1/traces/overview")) {
        return response({
          range: "24h",
          total: 1,
          completed: 1,
          successRate: 1,
          averageDurationMs: 25,
          inputTokens: 2,
          outputTokens: 3,
          totalTokens: 5,
          buckets: [],
        });
      }
      if (url === "/api/v1/traces/trace-1") {
        return response({
          traceId: "trace-1",
          runId: "run-1",
          agentId: "agent-1",
          sessionId: "session-1",
          runtimeType: "codex",
          model: "demo-model",
          status: "completed",
          startedAt: "2026-08-17T00:00:00Z",
          durationMs: 25,
          totalTokens: 5,
          usageReported: false,
          spanCount: 1,
          rootSpanId: "span-1",
          metrics: {
            inputTokens: 2,
            outputTokens: 3,
            totalTokens: 5,
            usageReported: false,
            usageSource: "gen_ai.usage",
          },
          spans: [{
            spanId: "span-1",
            name: "run",
            kind: "INTERNAL",
            status: "OK",
            startTimeUnixNano: "1",
            endTimeUnixNano: "2",
            durationMs: 25,
          }],
        });
      }
      if (url.startsWith("/api/v1/traces?")) {
        return response({
          items: [{
            traceId: "trace-1",
            runId: "run-1",
            agentId: "agent-1",
            sessionId: "session-1",
            runtimeType: "codex",
            model: "demo-model",
            status: "completed",
            startedAt: "2026-08-17T00:00:00Z",
            durationMs: 25,
            totalTokens: 5,
            usageReported: false,
            spanCount: 1,
          }],
          total: 1,
          nextCursor: null,
        });
      }
      if (url.startsWith("/api/v1/sessions/session-1/events?")) {
        return response({
          items: [{
            projectionVersion: 1,
            seqId: 1,
            eventId: "evt-1",
            recordId: "tool:call-1",
            type: "tool.call.end",
            category: "tool",
            turnId: "turn-1",
            stepId: "step-1",
            timestamp: 1,
            status: "completed",
            durationMs: 12,
            summary: "search",
            details: {},
          }],
          page: { oldestSeqId: 1, latestSeqId: 1, hasMore: false },
        });
      }
      if (url === "/api/v1/sessions/session-1:export") {
        if (String(init?.body).includes('"download":true')) return downloadResponse();
        return response({
          path: ".agentkit/exports/session-1-run-1.jsonl",
          eventCount: 3,
          firstSeqId: 1,
          lastSeqId: 3,
          exportedThroughSeqId: 3,
        });
      }
      return response({});
    });
  });

  afterEach(() => {
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it("shows actual token counters even when a legacy usage flag is absent", async () => {
    const user = userEvent.setup();
    render(<ObservabilityPage refreshTick={0} />);

    const table = await screen.findByRole("table", { name: "Trace 列表" });
    expect(within(table).getByText("5")).toBeInTheDocument();
    expect(within(table).queryByText("未上报")).not.toBeInTheDocument();

    await user.click(within(table).getByRole("button", { name: "查看详情" }));
    const metrics = await screen.findByRole("region", { name: "Trace 指标" });
    expect(within(metrics).getByText("5")).toBeInTheDocument();
    expect(within(metrics).getByText("2 输入 · 3 输出")).toBeInTheDocument();
  });

  it.each([
    {
      label: "input only",
      inputTokens: 2,
      outputTokens: null,
      listText: "2 输入",
      detailText: "2 输入 · — 输出",
    },
    {
      label: "output only",
      inputTokens: null,
      outputTokens: 3,
      listText: "3 输出",
      detailText: "— 输入 · 3 输出",
    },
  ])("shows partial token usage when $label is reported", async ({
    inputTokens,
    outputTokens,
    listText,
    detailText,
  }) => {
    vi.mocked(apiFetch).mockImplementation(input => {
      const url = String(input);
      if (url.startsWith("/api/v1/agents")) return response({ items: [] });
      if (url.startsWith("/api/v1/traces/overview")) {
        return response({
          range: "24h",
          total: 1,
          completed: 1,
          successRate: 1,
          averageDurationMs: 25,
          inputTokens: inputTokens || 0,
          outputTokens: outputTokens || 0,
          totalTokens: 0,
          buckets: [],
        });
      }
      if (url === "/api/v1/traces/trace-partial") {
        return response({
          traceId: "trace-partial",
          runId: "run-partial",
          agentId: "agent-1",
          sessionId: "session-partial",
          runtimeType: "codex",
          model: "demo-model",
          status: "completed",
          startedAt: "2026-08-17T00:00:00Z",
          durationMs: 25,
          inputTokens,
          outputTokens,
          totalTokens: null,
          usageReported: true,
          spanCount: 1,
          rootSpanId: "span-1",
          metrics: {
            inputTokens,
            outputTokens,
            totalTokens: null,
            usageReported: true,
            usageSource: "gen_ai.usage",
          },
          spans: [{
            spanId: "span-1",
            name: "run",
            kind: "INTERNAL",
            status: "OK",
            startTimeUnixNano: "1",
            endTimeUnixNano: "2",
            durationMs: 25,
          }],
        });
      }
      if (url.startsWith("/api/v1/traces?")) {
        return response({
          items: [{
            traceId: "trace-partial",
            runId: "run-partial",
            agentId: "agent-1",
            sessionId: "session-partial",
            runtimeType: "codex",
            model: "demo-model",
            status: "completed",
            startedAt: "2026-08-17T00:00:00Z",
            durationMs: 25,
            inputTokens,
            outputTokens,
            totalTokens: null,
            usageReported: true,
            spanCount: 1,
          }],
          total: 1,
          nextCursor: null,
        });
      }
      return response({});
    });
    const user = userEvent.setup();
    render(<ObservabilityPage refreshTick={0} />);

    const table = await screen.findByRole("table", { name: "Trace 列表" });
    expect(within(table).getByText(listText)).toBeInTheDocument();
    expect(within(table).queryByText("未上报")).not.toBeInTheDocument();

    await user.click(within(table).getByRole("button", { name: "查看详情" }));
    const metrics = await screen.findByRole("region", { name: "Trace 指标" });
    expect(within(metrics).getByText("部分上报")).toBeInTheDocument();
    expect(within(metrics).getByText(detailText)).toBeInTheDocument();
  });

  it("does not present incomplete billing-span counters as a complete total", async () => {
    vi.mocked(apiFetch).mockImplementation(input => {
      const url = String(input);
      if (url.startsWith("/api/v1/agents")) return response({ items: [] });
      if (url.startsWith("/api/v1/traces/overview")) {
        return response({
          range: "24h",
          total: 1,
          completed: 1,
          successRate: 1,
          averageDurationMs: 25,
          inputTokens: 120,
          outputTokens: 10,
          totalTokens: 0,
          buckets: [],
        });
      }
      const usageCompleteness = {
        inputTokens: true,
        outputTokens: false,
        totalTokens: false,
        cachedInputTokens: false,
        reasoningOutputTokens: false,
      };
      if (url === "/api/v1/traces/trace-incomplete") {
        return response({
          traceId: "trace-incomplete",
          runId: "run-incomplete",
          agentId: "agent-1",
          sessionId: "session-incomplete",
          runtimeType: "codex",
          model: "demo-model",
          status: "completed",
          startedAt: "2026-08-17T00:00:00Z",
          durationMs: 25,
          inputTokens: 120,
          outputTokens: 10,
          totalTokens: null,
          usageReported: true,
          usageCompleteness,
          spanCount: 2,
          rootSpanId: "span-1",
          metrics: {
            inputTokens: 120,
            outputTokens: 10,
            totalTokens: null,
            usageReported: true,
            usageSource: "gen_ai.usage",
            usageCompleteness,
          },
          spans: [{
            spanId: "span-1",
            name: "run",
            kind: "INTERNAL",
            status: "OK",
            startTimeUnixNano: "1",
            endTimeUnixNano: "2",
            durationMs: 25,
          }],
        });
      }
      if (url.startsWith("/api/v1/traces?")) {
        return response({
          items: [{
            traceId: "trace-incomplete",
            runId: "run-incomplete",
            agentId: "agent-1",
            sessionId: "session-incomplete",
            runtimeType: "codex",
            model: "demo-model",
            status: "completed",
            startedAt: "2026-08-17T00:00:00Z",
            durationMs: 25,
            inputTokens: 120,
            outputTokens: 10,
            totalTokens: null,
            usageReported: true,
            usageCompleteness,
            spanCount: 2,
          }],
          total: 1,
          nextCursor: null,
        });
      }
      return response({});
    });
    const user = userEvent.setup();
    render(<ObservabilityPage refreshTick={0} />);

    const table = await screen.findByRole("table", { name: "Trace 列表" });
    expect(within(table).getByText("部分上报")).toBeInTheDocument();
    expect(within(table).queryByText("130")).not.toBeInTheDocument();

    await user.click(within(table).getByRole("button", { name: "查看详情" }));
    const metrics = await screen.findByRole("region", { name: "Trace 指标" });
    expect(within(metrics).getByText("部分上报")).toBeInTheDocument();
    expect(within(metrics).getByText("120 输入 · ≥10 输出")).toBeInTheDocument();
  });

  it("switches an active trace between spans and its canonical trajectory", async () => {
    const user = userEvent.setup();
    render(<ObservabilityPage refreshTick={0} />);

    await user.click(await screen.findByRole("button", { name: "查看详情" }));
    expect(await screen.findByRole("tab", { name: "Spans" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    await user.click(screen.getByRole("tab", { name: "轨迹" }));

    await user.click(await screen.findByRole("button", { name: /search.*completed.*12 ms/i }));

    expect(screen.getByRole("complementary", { name: "轨迹详情" })).toHaveTextContent("search");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    const expand = screen.getByRole("button", { name: "放大详情" });
    await user.click(expand);
    expect(expand).toHaveAttribute("aria-pressed", "true");
    await user.click(screen.getByRole("button", { name: "收起详情" }));
    expect(screen.getByRole("button", { name: "展开详情" })).toBeVisible();
  });

  it("exports the active trace session log", async () => {
    const user = userEvent.setup();
    const write = vi.fn();
    const close = vi.fn();
    const showSaveFilePicker = vi.fn().mockResolvedValue({
      name: "chosen-session.jsonl",
      createWritable: vi.fn().mockResolvedValue({ write, close }),
    });
    vi.stubGlobal("showSaveFilePicker", showSaveFilePicker);
    render(<ObservabilityPage refreshTick={0} />);

    await user.click(await screen.findByRole("button", { name: "查看详情" }));
    await user.click(await screen.findByRole("button", { name: "导出 Session Log" }));

    expect(showSaveFilePicker).toHaveBeenCalledWith(expect.objectContaining({
      suggestedName: expect.stringMatching(/^session-1-run-1-.*\.jsonl$/),
    }));
    expect(write).toHaveBeenCalledWith(expect.any(Blob));
    expect(close).toHaveBeenCalledOnce();
    expect(showToast).toHaveBeenCalledWith(
      "Session Log 已导出",
      "chosen-session.jsonl · 3 条事件",
    );
    expect(vi.mocked(apiFetch)).toHaveBeenCalledWith(
      "/api/v1/sessions/session-1:export",
      expect.objectContaining({
        method: "POST",
        body: expect.stringMatching(/"invocationId":"run-1".*"download":true/),
      }),
    );
  });

  it("does not export when save as is cancelled", async () => {
    const user = userEvent.setup();
    vi.stubGlobal(
      "showSaveFilePicker",
      vi.fn().mockRejectedValue(new DOMException("cancelled", "AbortError")),
    );
    render(<ObservabilityPage refreshTick={0} />);

    await user.click(await screen.findByRole("button", { name: "查看详情" }));
    await user.click(await screen.findByRole("button", { name: "导出 Session Log" }));

    expect(vi.mocked(apiFetch).mock.calls).not.toContainEqual([
      "/api/v1/sessions/session-1:export",
      expect.anything(),
    ]);
    expect(showToast).not.toHaveBeenCalledWith(
      "Session Log 导出失败",
      expect.anything(),
      "error",
    );
  });
});

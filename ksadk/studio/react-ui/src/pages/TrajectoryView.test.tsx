import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { apiFetch } from "../api";
import { TrajectoryDetail, TrajectoryView } from "./TrajectoryView";
import type { TrajectoryEvent, TrajectoryRecord } from "./trajectory";

vi.mock("../api", () => ({ apiFetch: vi.fn() }));

function event(
  seqId: number,
  recordId: string,
  overrides: Partial<TrajectoryEvent> = {},
): TrajectoryEvent {
  return {
    projectionVersion: 1,
    seqId,
    eventId: `evt-${seqId}`,
    recordId,
    type: "run.progress",
    category: "system",
    turnId: null,
    stepId: null,
    timestamp: seqId,
    status: "completed",
    durationMs: null,
    summary: `event ${seqId}`,
    details: {},
    ...overrides,
  };
}

class FakeEventSource {
  static instances: FakeEventSource[] = [];
  readonly url: string;
  closed = false;
  listeners = new Map<string, (event: MessageEvent) => void>();

  constructor(url: string | URL) {
    this.url = String(url);
    FakeEventSource.instances.push(this);
  }

  addEventListener(name: string, listener: EventListener) {
    this.listeners.set(name, listener as (event: MessageEvent) => void);
  }

  close() {
    this.closed = true;
  }

  emit(value: TrajectoryEvent) {
    this.listeners.get("runtime_event")?.(
      new MessageEvent("runtime_event", { data: JSON.stringify(value) }),
    );
  }
}

const pageItems = [
  event(7, "turn:turn-1", {
    type: "turn.completed",
    turnId: "turn-1",
    durationMs: 75,
    summary: "Turn 1",
  }),
  event(8, "step:step-1", {
    type: "step.completed",
    turnId: "turn-1",
    stepId: "step-1",
    durationMs: 75,
    summary: "Step 1",
    details: { step_index: 1 },
  }),
  event(9, "tool:call-1", {
    type: "tool.call.end",
    category: "tool",
    turnId: "turn-1",
    stepId: "step-1",
    durationMs: 42,
    summary: "search",
  }),
  event(10, "assistant:step-1", {
    type: "assistant.message",
    category: "assistant",
    turnId: "turn-1",
    stepId: "step-1",
    durationMs: null,
    status: "running",
    summary: "Message",
    details: {
      model: "demo-model",
      usage: { input_tokens: 3, output_tokens: 2, reasoning_tokens: 1 },
    },
  }),
];

describe("TrajectoryView", () => {
  beforeEach(() => {
    FakeEventSource.instances = [];
    vi.stubGlobal("EventSource", FakeEventSource);
    vi.mocked(apiFetch).mockResolvedValue(
      new Response(
        JSON.stringify({
          items: pageItems,
          page: { oldestSeqId: 7, latestSeqId: 9, hasMore: true },
        }),
        { status: 200 },
      ),
    );
  });

  afterEach(() => {
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it("uses Duration, Turns, and Calls as real timeline controls", async () => {
    const user = userEvent.setup();
    render(<TrajectoryView sessionId="session-1" invocationId="run-1" />);

    expect(await screen.findByRole("button", { name: /search.*completed.*42 ms/i })).toBeVisible();
    expect(screen.getByRole("button", { name: /demo-model.*running.*不可用/i })).toBeVisible();
    expect(screen.getByText("Turn 1 · Step 1")).toBeVisible();
    expect(screen.queryByRole("button", { name: /Turn 1/i })).not.toBeInTheDocument();
    const message = screen.getByRole("button", { name: /demo-model.*running/i });
    expect(within(message).getByText("3")).toBeVisible();
    expect(within(message).getByText("2")).toBeVisible();
    expect(within(message).getByText("1")).toBeVisible();
    expect(screen.getByRole("button", { name: "Duration" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    expect(screen.getByText("Input")).toBeVisible();
    expect(screen.getByText("Model")).toBeVisible();
    expect(screen.getByText("Tools")).toBeVisible();

    await user.click(screen.getByRole("button", { name: "Calls" }));
    expect(screen.queryByRole("button", { name: /search.*completed/i })).not.toBeInTheDocument();
    expect(screen.queryByTitle("search")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Calls" })).toHaveAttribute("aria-pressed", "false");
    await user.click(screen.getByRole("button", { name: "Calls" }));
    expect(screen.getByRole("button", { name: /search.*completed/i })).toBeVisible();

    await user.click(screen.getByRole("button", { name: "Turns" }));
    expect(screen.queryByRole("button", { name: /demo-model.*running/i })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Turns" })).toHaveAttribute("aria-pressed", "false");

    await user.click(screen.getByRole("button", { name: "Duration" }));
    expect(screen.getByLabelText("轨迹时间带")).toHaveAttribute("data-mode", "equal");
  });

  it("reports the selected record for the shared trajectory detail panel", async () => {
    vi.mocked(apiFetch).mockResolvedValueOnce(
      new Response(
        JSON.stringify({
          items: [event(10, "assistant:step-1", {
            type: "assistant.message",
            category: "assistant",
            summary: "Message",
            details: { output: "你好", reasoning: "先分析" },
            source: {
              event_type: "text.completed",
              event_id: "evt-10",
              payload: { text: "你好" },
            },
          })],
          page: { oldestSeqId: 10, latestSeqId: 10, hasMore: false },
        }),
        { status: 200 },
      ),
    );
    const user = userEvent.setup();
    const onSelectionChange = vi.fn();
    render(<TrajectoryView sessionId="session-1" onSelectionChange={onSelectionChange} />);

    await user.click(await screen.findByRole("button", { name: /Message/i }));
    await waitFor(() => expect(onSelectionChange).toHaveBeenLastCalledWith(
      expect.objectContaining({ recordId: "assistant:step-1" }),
    ));
    const selected = onSelectionChange.mock.lastCall?.[0] as TrajectoryRecord;
    render(<TrajectoryDetail record={selected} />);
    await user.click(screen.getByRole("tab", { name: "Preview" }));

    expect(screen.getByText('"你好"')).toBeVisible();
    expect(screen.getByText('"先分析"')).toBeVisible();
    await user.click(screen.getByRole("tab", { name: "Raw Events" }));
    expect(screen.getByText(/"event_type": "text.completed"/)).toBeVisible();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("pauses tail following after scrolling away and restores it on command", async () => {
    const user = userEvent.setup();
    render(<TrajectoryView sessionId="session-1" />);
    const ledger = await screen.findByRole("log", { name: "轨迹事件" });
    Object.defineProperties(ledger, {
      scrollHeight: { configurable: true, value: 1000 },
      clientHeight: { configurable: true, value: 200 },
      scrollTop: { configurable: true, writable: true, value: 100 },
    });

    fireEvent.scroll(ledger);
    const latest = await screen.findByRole("button", { name: "回到最新" });
    await user.click(latest);

    expect(screen.queryByRole("button", { name: "回到最新" })).not.toBeInTheDocument();
    expect(ledger.scrollTop).toBe(ledger.scrollHeight);
  });

  it("opens from the persisted cursor, folds live records, and closes on unmount", async () => {
    const { unmount } = render(<TrajectoryView sessionId="session-1" />);
    await screen.findByRole("button", { name: /search.*completed/i });
    const source = FakeEventSource.instances[0];

    expect(source.url).toContain("/api/v1/sessions/session-1/events/stream?afterSeqId=10");
    act(() => {
      source.emit(
        event(11, "assistant:step-1", {
          type: "model.call.end",
          category: "assistant",
          durationMs: 120,
          summary: "demo-model",
        }),
      );
    });

    await waitFor(() => {
      expect(screen.getByRole("button", { name: /demo-model.*completed.*120 ms/i })).toBeVisible();
    });
    expect(screen.queryByRole("button", { name: /demo-model.*running/i })).not.toBeInTheDocument();

    unmount();
    expect(source.closed).toBe(true);
  });

  it("reports a failed cursor gap backfill", async () => {
    vi.mocked(apiFetch)
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            items: [event(8, "tool:8", { category: "tool", summary: "tool 8" })],
            page: { oldestSeqId: 8, latestSeqId: 8, hasMore: false },
          }),
          { status: 200 },
        ),
      )
      .mockResolvedValueOnce(new Response("{}", { status: 500 }));
    render(<TrajectoryView sessionId="session-1" />);
    await screen.findByRole("button", { name: /tool 8/i });

    act(() => {
      FakeEventSource.instances[0].emit(
        event(10, "tool:10", { category: "tool", summary: "tool 10" }),
      );
    });

    expect(
      await screen.findByText("实时轨迹存在序号缺口，补拉失败"),
    ).toBeVisible();
  });

  it("replays a cursor gap through the same reducer", async () => {
    vi.mocked(apiFetch)
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            items: [event(8, "tool:8", { category: "tool", summary: "tool 8" })],
            page: { oldestSeqId: 8, latestSeqId: 8, hasMore: false },
          }),
          { status: 200 },
        ),
      )
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            items: [event(9, "tool:9", { category: "tool", summary: "tool 9" })],
            page: { oldestSeqId: 9, latestSeqId: 9, hasMore: false },
          }),
          { status: 200 },
        ),
      );
    render(<TrajectoryView sessionId="session-1" />);
    await screen.findByRole("button", { name: /tool 8/i });

    act(() => {
      FakeEventSource.instances[0].emit(
        event(10, "tool:10", { category: "tool", summary: "tool 10" }),
      );
    });

    expect(await screen.findByRole("button", { name: /tool 9/i })).toBeVisible();
    expect(screen.getByRole("button", { name: /tool 10/i })).toBeVisible();
  });

  it("omits token columns when no semantic node reports usage", async () => {
    vi.mocked(apiFetch).mockResolvedValueOnce(
      new Response(
        JSON.stringify({
          items: [event(1, "user:msg-1", {
            type: "user.message",
            category: "user",
            summary: "检查项目",
            details: { text: "检查项目" },
          })],
          page: { oldestSeqId: 1, latestSeqId: 1, hasMore: false },
        }),
        { status: 200 },
      ),
    );

    render(<TrajectoryView sessionId="session-1" />);

    expect(await screen.findByRole("button", { name: /检查项目/i })).toBeVisible();
    expect(screen.queryByText("Input Tokens")).not.toBeInTheDocument();
    expect(screen.queryByText("Output Tokens")).not.toBeInTheDocument();
    expect(screen.getByText(/0 Turn/)).toBeVisible();
    expect(screen.getByRole("button", { name: "Duration" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Calls" })).toBeDisabled();
  });
});

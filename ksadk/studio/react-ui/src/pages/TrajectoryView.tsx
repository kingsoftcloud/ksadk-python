import { Fragment, useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Archive,
  Bot,
  Braces,
  ChevronUp,
  CircleDot,
  FileBox,
  MessageSquare,
  ShieldCheck,
  Wrench,
} from "lucide-react";
import { apiFetch } from "../api";
import {
  mergeTrajectory,
  prependTrajectory,
  trajectoryState,
  type TrajectoryEvent,
  type TrajectoryRecord,
  type TrajectoryState,
} from "./trajectory";

type DetailTab = "summary" | "preview" | "raw" | "source";

type TrajectoryPage = {
  items: TrajectoryEvent[];
  page: {
    oldestSeqId: number | null;
    latestSeqId: number | null;
    hasMore: boolean;
  };
};

function formatDuration(value: number | null): string {
  if (value === null || Number.isNaN(value)) return "不可用";
  if (value < 1000) return `${Math.round(value)} ms`;
  return `${(value / 1000).toFixed(value < 10000 ? 2 : 1)} s`;
}

function eventIcon(category: TrajectoryEvent["category"]) {
  if (category === "tool") return <Wrench size={15} />;
  if (category === "assistant") return <Bot size={15} />;
  if (category === "context") return <Braces size={15} />;
  if (category === "user") return <MessageSquare size={15} />;
  if (category === "approval") return <ShieldCheck size={15} />;
  if (category === "artifact") return <FileBox size={15} />;
  return <CircleDot size={15} />;
}

function eventLabel(event: TrajectoryEvent): string {
  return [
    rowName(event),
    event.status || "未上报",
    formatDuration(event.durationMs),
  ].join(" · ");
}

function record(value: unknown): Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {};
}

function rowName(event: TrajectoryEvent): string {
  const value = event.details.name ?? event.details.model;
  return typeof value === "string" && value ? value : event.summary;
}

function tokenValue(event: TrajectoryEvent, key: string): string {
  const value = record(event.details.usage)[key];
  return typeof value === "number" ? String(value) : "-";
}

function categoryLabel(category: TrajectoryEvent["category"]): string {
  return {
    user: "User",
    context: "Context",
    assistant: "Assistant",
    tool: "Tool",
    approval: "Approval",
    artifact: "Artifact",
    system: "System",
  }[category];
}

function laneFor(item: TrajectoryRecord): "input" | "model" | "tools" | null {
  if (item.category === "user" || item.category === "context") return "input";
  if (item.category === "assistant") return "model";
  if (item.category === "tool") return "tools";
  return null;
}

function initialPageUrl(sessionId: string, invocationId?: string): string {
  const query = new URLSearchParams({ limit: "100" });
  if (invocationId) query.set("invocationId", invocationId);
  return `/api/v1/sessions/${encodeURIComponent(sessionId)}/events?${query}`;
}

export function TrajectoryDetail({ record: selected }: { record: TrajectoryRecord }) {
  const [detailTab, setDetailTab] = useState<DetailTab>(
    selected.category === "system" ? "source" : "summary",
  );
  return (
    <div className="trajectory-detail">
      <header className="trajectory-detail-heading">
        <strong>{rowName(selected)}</strong>
        <span>{categoryLabel(selected.category)} · {selected.status || "未上报"}</span>
      </header>
      <div className="trajectory-detail-tabs" role="tablist" aria-label="节点详情">
        {(["summary", "preview", "raw", "source"] as const).map((tab) => <button key={tab} type="button" role="tab" aria-selected={detailTab === tab} onClick={() => setDetailTab(tab)}>{tab === "summary" ? "Summary" : tab === "preview" ? "Preview" : tab === "raw" ? "Raw Events" : "Source"}</button>)}
      </div>
      {detailTab === "summary" && <>
        <section><h3>Timing</h3><pre>{JSON.stringify({ status: selected.status, durationMs: selected.durationMs, ttftMs: selected.details.ttft_ms ?? null, startedAt: selected.startedAt, endedAt: selected.endedAt }, null, 2)}</pre></section>
        {selected.details.usage !== undefined && <section><h3>Usage</h3><pre>{JSON.stringify(selected.details.usage, null, 2)}</pre></section>}
      </>}
      {detailTab === "preview" && <>
        <section><h3>Input</h3><pre>{JSON.stringify(selected.details.args ?? selected.details.input ?? (selected.category === "user" ? selected.details.text : null), null, 2)}</pre></section>
        <section><h3>Output</h3><pre>{JSON.stringify(selected.details.result ?? selected.details.output ?? null, null, 2)}</pre></section>
        <section><h3>Reasoning</h3><pre>{JSON.stringify(selected.details.reasoning ?? null, null, 2)}</pre></section>
      </>}
      {detailTab === "raw" && <section><h3>Raw Events</h3><pre>{JSON.stringify(selected.sourceEvents.map((item) => item.source ?? item), null, 2)}</pre></section>}
      {detailTab === "source" && <section><h3>Source</h3><pre>{JSON.stringify({ recordId: selected.recordId, eventIds: selected.sourceEvents.map((item) => item.eventId), seqIds: selected.sourceEvents.map((item) => item.seqId), turnId: selected.turnId, stepId: selected.stepId }, null, 2)}</pre></section>}
    </div>
  );
}

export function TrajectoryView({
  sessionId,
  invocationId,
  onSelectionChange,
}: {
  sessionId: string;
  invocationId?: string;
  onSelectionChange?: (record: TrajectoryRecord | null) => void;
}) {
  const [state, setState] = useState<TrajectoryState>(() => trajectoryState());
  const [durationScaled, setDurationScaled] = useState(true);
  const [turnsExpanded, setTurnsExpanded] = useState(true);
  const [callsExpanded, setCallsExpanded] = useState(true);
  const [systemExpanded, setSystemExpanded] = useState(false);
  const [selectedRecordId, setSelectedRecordId] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const ledgerRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    let disposed = false;
    let source: EventSource | null = null;
    setLoading(true);
    setError("");
    setState(trajectoryState());
    setSelectedRecordId(null);

    void apiFetch(initialPageUrl(sessionId, invocationId))
      .then(async (response) => {
        if (!response.ok) throw new Error(`轨迹加载失败（${response.status}）`);
        return response.json() as Promise<TrajectoryPage>;
      })
      .then((page) => {
        if (disposed) return;
        const next = trajectoryState(page.items || [], Boolean(page.page?.hasMore));
        setState(next);
        setLoading(false);
        const query = new URLSearchParams({ afterSeqId: String(next.lastSeqId) });
        if (invocationId) query.set("invocationId", invocationId);
        source = new EventSource(
          `/api/v1/sessions/${encodeURIComponent(sessionId)}/events/stream?${query}`,
        );
        source.addEventListener("runtime_event", (message) => {
          try {
            const incoming = JSON.parse((message as MessageEvent).data) as TrajectoryEvent;
            setState((current) => mergeTrajectory(current, incoming, !invocationId));
          } catch {
            setError("实时轨迹包含无法解析的事件");
          }
        });
      })
      .catch((reason) => {
        if (disposed) return;
        setLoading(false);
        setError(reason instanceof Error ? reason.message : "轨迹加载失败");
      });

    return () => {
      disposed = true;
      source?.close();
    };
  }, [invocationId, sessionId]);

  useEffect(() => {
    if (!state.gap) return;
    let disposed = false;
    const gap = state.gap;
    const query = new URLSearchParams({
      limit: "500",
      beforeSeqId: String(gap.beforeSeqId),
    });
    if (invocationId) query.set("invocationId", invocationId);
    void apiFetch(`/api/v1/sessions/${encodeURIComponent(sessionId)}/events?${query}`)
      .then((response) => {
        if (!response.ok) throw new Error(`轨迹补拉失败（${response.status}）`);
        return response.json() as Promise<TrajectoryPage>;
      })
      .then((page) => {
        if (disposed) return;
        const missing = (page.items || [])
          .filter((item) => item.seqId > gap.afterSeqId)
          .sort((left, right) => left.seqId - right.seqId);
        setState((current) => missing.reduce(
          (next, event) => mergeTrajectory(next, event),
          current,
        ));
      })
      .catch(() => {
        if (!disposed) setError("实时轨迹存在序号缺口，补拉失败");
      });
    return () => { disposed = true; };
  }, [invocationId, sessionId, state.gap]);

  useEffect(() => {
    const ledger = ledgerRef.current;
    if (ledger && state.followTail) ledger.scrollTop = ledger.scrollHeight;
  }, [state.followTail, state.items]);

  const loadOlder = useCallback(async () => {
    const oldest = Math.min(...state.bySeq.keys());
    if (!Number.isFinite(oldest)) return;
    const query = new URLSearchParams({ limit: "100", beforeSeqId: String(oldest) });
    if (invocationId) query.set("invocationId", invocationId);
    const response = await apiFetch(
      `/api/v1/sessions/${encodeURIComponent(sessionId)}/events?${query}`,
    );
    if (!response.ok) {
      setError("更早轨迹加载失败");
      return;
    }
    const page = await response.json() as TrajectoryPage;
    setState((current) => prependTrajectory(current, page.items || [], page.page.hasMore));
  }, [invocationId, sessionId, state.bySeq]);

  const layout = useMemo(() => {
    const turns = new Map<string, string>();
    const steps = new Map<string, string>();
    for (const item of state.items) {
      if (item.recordId.startsWith("turn:") && item.turnId) {
        const index = item.details.turn_index;
        turns.set(item.turnId, typeof index === "number" ? `Turn ${index}` : item.summary);
      }
      if (item.recordId.startsWith("step:") && item.stepId) {
        const index = item.details.step_index;
        steps.set(item.stepId, typeof index === "number" ? `Step ${index}` : item.summary);
      }
    }
    const semanticItems = state.items.filter((item) => item.category !== "system");
    const visibleItems = callsExpanded
      ? semanticItems
      : semanticItems.filter((item) => item.category !== "tool");
    const groups = new Map<string, { id: string; label: string; items: TrajectoryRecord[] }>();
    for (const item of visibleItems) {
      const key = item.turnId || "run";
      const label = item.turnId ? turns.get(item.turnId) || "Turn" : "Run";
      const group = groups.get(key) || { id: key, label, items: [] };
      group.items.push(item);
      groups.set(key, group);
    }
    const duration = Math.max(0, ...state.items.map((item) => item.durationMs || 0));
    return {
      groups: [...groups.values()],
      semanticItems,
      systemItems: state.items.filter((item) => item.category === "system"),
      steps,
      turns: turns.size,
      hasUsage: semanticItems.some((item) => Object.keys(record(item.details.usage)).length > 0),
      duration,
      calls: semanticItems.filter((item) => item.category === "tool").length,
    };
  }, [callsExpanded, state.items]);

  const selected = selectedRecordId
    ? state.items.find((item) => item.recordId === selectedRecordId) || null
    : null;

  useEffect(() => {
    onSelectionChange?.(selected);
  }, [onSelectionChange, selected]);

  const timeline = useMemo(() => {
    const items = layout.semanticItems.filter(
      (item) => laneFor(item) && (callsExpanded || item.category !== "tool"),
    );
    const start = Math.min(...items.map((item) => item.startedAt));
    const end = Math.max(...items.map((item) => item.endedAt ?? item.startedAt));
    const span = Math.max(end - start, 0.001);
    return { items, start, span };
  }, [callsExpanded, layout.semanticItems]);

  const restoreTail = () => {
    const ledger = ledgerRef.current;
    if (ledger) ledger.scrollTop = ledger.scrollHeight;
    setState((current) => ({ ...current, followTail: true }));
  };

  return (
    <section className="trajectory-view" aria-label="实时轨迹">
      <header className="trajectory-toolbar">
        <div className="segmented-control" aria-label="轨迹显示控制">
          <button type="button" className={durationScaled ? "selected" : ""} aria-pressed={durationScaled} disabled={timeline.items.length <= 1} onClick={() => setDurationScaled((value) => !value)}>Duration</button>
          <button type="button" className={turnsExpanded ? "selected" : ""} aria-pressed={turnsExpanded} onClick={() => setTurnsExpanded((value) => !value)}>Turns</button>
          <button type="button" className={callsExpanded ? "selected" : ""} aria-pressed={callsExpanded} disabled={layout.calls === 0} onClick={() => setCallsExpanded((value) => !value)}>Calls</button>
        </div>
        <strong className="trajectory-summary-value">
          {layout.turns} Turn · {layout.calls} Call · {formatDuration(layout.duration)}
        </strong>
        {state.hasMore && (
          <button className="button tertiary small" type="button" onClick={() => void loadOlder()}>
            <ChevronUp size={14} />
            <span>加载更早</span>
          </button>
        )}
      </header>

      {error && <div className="trajectory-status error">{error}</div>}
      {loading && <div className="trajectory-status">正在读取轨迹...</div>}
      {!loading && !layout.semanticItems.length && !layout.systemItems.length && (
        <div className="trajectory-status">当前 Session 没有轨迹事件。</div>
      )}

      {!!timeline.items.length && (
        <div className="trajectory-timeline" aria-label="轨迹时间带" data-mode={durationScaled ? "duration" : "equal"}>
          {(["input", "model", "tools"] as const).map((lane) => (
            <div className="trajectory-lane" key={lane}>
              <span>{lane === "input" ? "Input" : lane === "model" ? "Model" : "Tools"}</span>
              <div>
                {timeline.items.filter((item) => laneFor(item) === lane).map((item, index, laneItems) => {
                  const left = durationScaled
                    ? ((item.startedAt - timeline.start) / timeline.span) * 100
                    : (index / Math.max(laneItems.length, 1)) * 100;
                  const width = durationScaled
                    ? Math.max(2, (((item.endedAt ?? item.startedAt) - item.startedAt) / timeline.span) * 100)
                    : 100 / Math.max(laneItems.length, 1);
                  return <i key={item.recordId} data-category={item.category} title={rowName(item)} style={{ left: `${left}%`, width: `${Math.min(width, 100 - left)}%` }} />;
                })}
              </div>
            </div>
          ))}
        </div>
      )}

      <div
        ref={ledgerRef}
        className="trajectory-ledger"
        role="log"
        aria-label="轨迹事件"
        onScroll={(event) => {
          const target = event.currentTarget;
          const followTail = target.scrollHeight - target.scrollTop - target.clientHeight < 48;
          setState((current) => current.followTail === followTail
            ? current
            : { ...current, followTail });
        }}
      >
        {!!layout.semanticItems.length && (
          <div className="trajectory-column-header" data-usage={layout.hasUsage} aria-hidden="true">
            <span>Event</span>
            {layout.hasUsage && <><span>Input Tokens</span><span>Output Tokens</span><span>Think</span></>}
            <span>Time</span>
          </div>
        )}
        {layout.groups.map((group) => (
          <Fragment key={group.id}>
            <div className="trajectory-turn-header"><strong>{group.label}</strong><span>{group.items.length} nodes</span></div>
            {turnsExpanded && group.items.map((item, index) => {
              const step = item.stepId ? layout.steps.get(item.stepId) || "Step" : "";
              const previous = index > 0 ? group.items[index - 1] : null;
              const previousStep = previous?.stepId ? layout.steps.get(previous.stepId) || "Step" : "";
              return <Fragment key={item.recordId}>
                {step && step !== previousStep && <div className="trajectory-group-label">{group.label} · {step}</div>}
                <button
                type="button"
                className="trajectory-row"
                data-usage={layout.hasUsage}
                data-category={item.category}
                data-status={item.status || "unknown"}
                aria-pressed={item.recordId === selectedRecordId}
                aria-label={eventLabel(item)}
                onClick={() => setSelectedRecordId(item.recordId)}
              >
                <span className="trajectory-row-icon">{eventIcon(item.category)}</span>
                <span className="trajectory-row-copy">
                  <strong>{rowName(item)}</strong>
                  <small>{categoryLabel(item.category)} · {item.status || "未上报"}</small>
                </span>
                {layout.hasUsage && <>
                  <span className="trajectory-row-metric">{tokenValue(item, "input_tokens")}</span>
                  <span className="trajectory-row-metric">{tokenValue(item, "output_tokens")}</span>
                  <span className="trajectory-row-metric">{tokenValue(item, "reasoning_tokens")}</span>
                </>}
                <span className="trajectory-row-duration">{formatDuration(item.durationMs)}</span>
              </button>
              </Fragment>;
            })}
          </Fragment>
        ))}
        {!!layout.systemItems.length && (
          <div className="trajectory-system-events">
            <button type="button" onClick={() => setSystemExpanded((value) => !value)} aria-expanded={systemExpanded}><Archive size={14} />System Events ({layout.systemItems.length})</button>
            {systemExpanded && layout.systemItems.map((item) => <button key={item.recordId} type="button" className="trajectory-system-row" aria-pressed={item.recordId === selectedRecordId} onClick={() => setSelectedRecordId(item.recordId)}>{item.type} · #{item.lastSeqId}</button>)}
          </div>
        )}
      </div>

      {!state.followTail && (
        <button className="button secondary small trajectory-latest" type="button" onClick={restoreTail}>
          回到最新
        </button>
      )}

    </section>
  );
}

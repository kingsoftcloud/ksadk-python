import { useEffect, useRef, useState } from "react";
import { Brain, ChevronRight } from "lucide-react";
import { LatestSummaryLine } from "./LatestSummaryLine";

function formatTokens(value: number): string {
  return Number.isFinite(value) ? Number(value).toLocaleString() : "—";
}

/**
 * Live thinking block for the conversation area.
 *
 * While the segment streams it shows the authorized summary as it is appended
 * with a static icon and animated label. Once it ends the label stops and text
 * keeps a manual disclosure choice. Time is explicitly browser-observed,
 * independent of the runtime's token accounting and unavailable timestamps.
 */
export function LiveThinking({ text, streaming, reasoningTokens }: {
  text: string;
  streaming: boolean;
  reasoningTokens?: number | null;
}) {
  const bodyRef = useRef<HTMLDivElement>(null);
  const follow = useRef(true);
  const started = useRef<number | null>(null);
  const [elapsed, setElapsed] = useState<number | null>(null);
  const [open, setOpen] = useState(false);
  const [paused, setPaused] = useState(false);
  useEffect(() => {
    // History loaded after completion has no observed start. Do not manufacture
    // a zero-second duration from the component's mount time.
    if (streaming && started.current === null) started.current = Date.now();
    if (started.current === null) return;
    const update = () => setElapsed(Math.max(0, (Date.now() - started.current!) / 1000));
    update();
    if (!streaming) return;
    const timer = setInterval(update, 1000);
    return () => clearInterval(timer);
  }, [streaming]);
  useEffect(() => {
    if (streaming && open && follow.current && bodyRef.current) bodyRef.current.scrollTop = bodyRef.current.scrollHeight;
  }, [streaming, text, open]);
  const body = <div className="harness-live-thinking-body" data-testid="live-thinking-text" ref={bodyRef}
    onScroll={event => { const node = event.currentTarget;
      follow.current = node.scrollHeight - node.clientHeight - node.scrollTop < 24;
      setPaused(!follow.current);
    }}>
    {text}
  </div>;
  // An execution fact without public text has nothing to expand. Do not fall
  // back to the old counter/disclosure row or invent a reasoning preview.
  if (!text.trim()) return <section className="harness-live-thinking" data-testid="live-thinking"
    data-streaming={streaming ? "true" : "false"} aria-label="思考过程">
    <div className="harness-live-thinking-head">
      <Brain className="harness-thinking-icon" size={16} aria-hidden="true" />
      <span className="harness-live-thinking-title" role={streaming ? "status" : undefined}>
        {streaming ? "正在思考" : "思考"}
      </span>
    </div>
  </section>;
  return (
    <section
      className="harness-live-thinking"
      data-testid="live-thinking"
      data-streaming={streaming ? "true" : "false"}
      aria-label="思考过程"
    >
      <details className="harness-live-thinking-details" open={open} onToggle={event => setOpen(event.currentTarget.open)}>
        <summary className="harness-live-thinking-head">
          <Brain className="harness-thinking-icon" size={16} aria-hidden="true" />
          <span className="harness-live-thinking-title" role={streaming ? "status" : undefined}>
            {streaming ? "正在思考" : "思考"}
          </span>
          {elapsed !== null && (!streaming || elapsed >= 1) && <span className="activity-duration" title="按本次页面观测到本段文字的时间计算；不代表完整模型请求耗时。">· {elapsed < 1 ? "少于 1 秒" : `${streaming ? "" : "持续了 "}${Math.ceil(elapsed)} 秒`}</span>}
          {!open && streaming && <LatestSummaryLine text={text} streaming={streaming} />}
          {reasoningTokens === null || reasoningTokens === undefined ? null : (
            <span className="harness-live-thinking-tokens" data-testid="live-thinking-tokens">
              思考 Token {formatTokens(reasoningTokens)}
            </span>
          )}
          <ChevronRight className="harness-live-thinking-chevron" size={13} aria-hidden="true" />
        </summary>
        {open && body}
        {paused && streaming && <button className="button tertiary small" type="button" onClick={() => {
          follow.current = true; setPaused(false);
          if (bodyRef.current) bodyRef.current.scrollTop = bodyRef.current.scrollHeight;
        }}>跟随最新思考</button>}
      </details>
    </section>
  );
}

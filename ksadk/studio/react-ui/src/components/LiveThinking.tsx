import { useEffect, useRef, useState } from "react";
import { ChevronRight } from "lucide-react";

function formatTokens(value: number): string {
  return Number.isFinite(value) ? Number(value).toLocaleString() : "—";
}

const THINKING_PREVIEW_LIMIT = 900;

export function thinkingPreview(value: string): { text: string; truncated: boolean } {
  const text = value.trim();
  if (text.length <= THINKING_PREVIEW_LIMIT) return { text, truncated: false };
  const tail = text.slice(-THINKING_PREVIEW_LIMIT);
  const boundary = Math.max(tail.indexOf("。"), tail.indexOf("\n"));
  return {
    text: `…${tail.slice(boundary >= 0 ? boundary + 1 : 0).trimStart()}`,
    truncated: true,
  };
}

/**
 * Live thinking block for the conversation area.
 *
 * While the turn streams it shows the reasoning text as it is appended and a
 * pulsing indicator.  Once reasoning ends the indicator stops and the text
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
  const started = useRef(Date.now());
  const [elapsed, setElapsed] = useState(0);
  const [open, setOpen] = useState(false);
  const [paused, setPaused] = useState(false);
  useEffect(() => {
    const update = () => setElapsed(Math.max(0, Math.floor((Date.now() - started.current) / 1000)));
    update();
    if (!streaming) return;
    const timer = setInterval(update, 1000);
    return () => clearInterval(timer);
  }, [streaming]);
  useEffect(() => {
    if (streaming && open && follow.current && bodyRef.current) bodyRef.current.scrollTop = bodyRef.current.scrollHeight;
  }, [streaming, text, open]);
  const preview = thinkingPreview(text);
  const body = <div className="harness-live-thinking-body" data-testid="live-thinking-text" ref={bodyRef}
    onScroll={event => { const node = event.currentTarget;
      follow.current = node.scrollHeight - node.clientHeight - node.scrollTop < 24;
      setPaused(!follow.current);
    }}>
    {preview.text}
    {preview.truncated && <p className="harness-live-thinking-note">仅显示最近一段实时思路，完整思考不会写入会话记录。</p>}
  </div>;
  return (
    <section
      className="harness-live-thinking"
      data-testid="live-thinking"
      data-streaming={streaming ? "true" : "false"}
      aria-label="思考过程"
    >
      <details className="harness-live-thinking-details" open={open} onToggle={event => setOpen(event.currentTarget.open)}>
        <summary className="harness-live-thinking-head">
          <span className="harness-live-thinking-pulse" data-testid="live-thinking-pulse" aria-hidden="true">
            <i /><i /><i />
          </span>
          <span className="activity-duration" title="从浏览器首次收到本段思考起计时，非模型运行耗时">观测 {elapsed}s</span>
          {!open && <span className="harness-thinking-latest">{text.trim().split("\n").filter(Boolean).at(-1)}</span>}
          <span className="harness-live-thinking-title" role={streaming ? "status" : undefined}>
            {streaming ? "正在思考…" : "已思考"}
          </span>
          {reasoningTokens === null || reasoningTokens === undefined ? null : (
            <span className="harness-live-thinking-tokens" data-testid="live-thinking-tokens">
              思考 Token {formatTokens(reasoningTokens)}
            </span>
          )}
          <ChevronRight className="harness-live-thinking-chevron" size={13} aria-hidden="true" />
        </summary>
        {body}
        {paused && streaming && <button className="button tertiary small" type="button" onClick={() => {
          follow.current = true; setPaused(false);
          if (bodyRef.current) bodyRef.current.scrollTop = bodyRef.current.scrollHeight;
        }}>跟随最新思考</button>}
      </details>
    </section>
  );
}

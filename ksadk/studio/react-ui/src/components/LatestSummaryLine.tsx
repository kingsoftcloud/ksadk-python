import { useEffect, useLayoutEffect, useRef, useState } from "react";

type Line = { key: number; text: string };

function latestLine(text: string): Line {
  const lines = text.split("\n");
  for (let key = lines.length - 1; key >= 0; key -= 1) {
    if (lines[key].trim()) return { key, text: lines[key] };
  }
  return { key: 0, text: "" };
}

/**
 * ZCode aac4755 reasoning.tsx / QueuedSummaryContent.tsx presentation policy:
 * refresh the current line in place, queue at most two new lines, animate for
 * 300 ms then hold for 500 ms; after a delayed timer show the newest line.
 * Uses CSS motion so the Studio host needs no second animation dependency.
 */
export function LatestSummaryLine({ text, streaming }: { text: string; streaming: boolean }) {
  const current = useRef<Line>(latestLine(text));
  const pending = useRef<Line[]>([]);
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const expected = useRef(0);
  const [active, setActive] = useState(current.current);
  const [outgoing, setOutgoing] = useState<Line | null>(null);
  const [reduced, setReduced] = useState(() => typeof matchMedia === "function"
    && matchMedia("(prefers-reduced-motion: reduce)").matches);
  const scroller = useRef<HTMLSpanElement>(null);

  useEffect(() => {
    if (typeof matchMedia !== "function") return;
    const query = matchMedia("(prefers-reduced-motion: reduce)");
    const update = () => setReduced(query.matches);
    query.addEventListener("change", update);
    return () => query.removeEventListener("change", update);
  }, []);
  useEffect(() => {
    const next = latestLine(text);
    if (reduced || !streaming) {
      clearTimeout(timer.current); timer.current = undefined;
      pending.current = []; current.current = next;
      setActive(next); setOutgoing(null);
      return;
    }
    if (next.key === current.current.key) {
      current.current = next; setActive(next);
      return;
    }
    const existing = pending.current.findIndex(line => line.key === next.key);
    if (existing >= 0) pending.current[existing] = next;
    else pending.current = pending.current.length ? [pending.current[0], next] : [next];

    const promote = (skipStale = false) => {
      const nextLine = skipStale
        ? pending.current.splice(0).at(-1)
        : pending.current.shift();
      if (!nextLine) { timer.current = undefined; return; }
      setOutgoing(current.current); current.current = nextLine; setActive(nextLine);
      expected.current = Date.now() + 800;
      timer.current = setTimeout(() => {
        setOutgoing(null);
        if (Date.now() - expected.current > 250) {
          promote(true); return;
        }
        timer.current = setTimeout(() => promote(Date.now() - expected.current > 250),
          Math.max(0, expected.current - Date.now()));
      }, 300);
    };
    if (timer.current === undefined) promote();
  }, [text, streaming, reduced]);
  useEffect(() => () => clearTimeout(timer.current), []);
  useLayoutEffect(() => {
    const node = scroller.current;
    if (!node) return;
    const follow = () => { node.scrollLeft = node.scrollWidth; };
    follow();
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(follow);
    observer.observe(node);
    if (node.firstElementChild) observer.observe(node.firstElementChild);
    return () => observer.disconnect();
  }, [active.text]);

  return <span className="harness-thinking-latest" data-reduced-motion={reduced}>
    {outgoing && <span key={`old-${outgoing.key}`} className="harness-summary-outgoing" aria-hidden="true">{outgoing.text}</span>}
    <span className="harness-summary-scroll" data-testid="thinking-line-scroll" ref={scroller}>
      <span key={active.key} className={outgoing ? "harness-summary-incoming" : undefined}>{active.text}</span>
    </span>
  </span>;
}

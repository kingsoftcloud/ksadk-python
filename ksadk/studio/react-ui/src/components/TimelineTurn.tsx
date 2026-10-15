import { useEffect, useLayoutEffect, useRef, useState, type ReactNode, type RefObject } from "react";

/** Keep the live tail mounted; window old, measured turns around the viewport. */
export function TimelineTurn({ id, root, heights, pinned, live, follow, children, revealed }: {
  id: string; root: RefObject<HTMLDivElement | null>; heights: Map<string, number>;
  pinned: boolean; live: boolean; follow: RefObject<boolean>; children: ReactNode; revealed: boolean;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const disclosures = useRef<boolean[]>([]);
  const canWindow = typeof IntersectionObserver !== "undefined" && typeof ResizeObserver !== "undefined";
  const [visible, setVisible] = useState(!canWindow || live || pinned);
  const mounted = visible || live || pinned;
  useLayoutEffect(() => {
    const row = ref.current;
    if (!mounted || !row) return;
    const details = Array.from(row.querySelectorAll("details"));
    details.forEach((node, index) => {
      if (disclosures.current[index] !== undefined) node.open = disclosures.current[index];
    });
    const remember = () => { disclosures.current = details.map(node => node.open); };
    row.addEventListener("toggle", remember, true);
    return () => row.removeEventListener("toggle", remember, true);
  }, [mounted]);
  useEffect(() => {
    if (!canWindow || !ref.current || !root.current) return;
    const observer = new IntersectionObserver(([entry]) => setVisible(entry.isIntersecting), {
      root: root.current, rootMargin: "1000px 0px",
    });
    observer.observe(ref.current);
    return () => observer.disconnect();
  }, [canWindow, root]);
  useLayoutEffect(() => {
    if (!ref.current || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(() => {
      const row = ref.current;
      const scroll = root.current;
      if (!row || !scroll) return;
      const height = row.getBoundingClientRect().height;
      if (mounted) heights.set(id, height);
      if (follow.current) {
        scroll.scrollTop = scroll.scrollHeight;
      }
    });
    observer.observe(ref.current);
    return () => observer.disconnect();
  }, [id, mounted, root, heights, follow]);
  return <div ref={ref} className="harness-turn" tabIndex={-1} data-message-id={id}
    data-search-target={revealed || undefined} data-windowed={!mounted || undefined}
    style={mounted ? undefined : { height: heights.get(id) ?? 160 }}>
    {mounted ? children : null}
  </div>;
}

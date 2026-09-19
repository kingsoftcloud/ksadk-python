import { useEffect, useState } from "react";
import { apiFetch } from "../api";

export type WorkspaceContribution = {
  id: string;
  label: string;
  pluginId?: string;
  order?: number;
};

/** Navigation comes from live DSH contributions; no domain page is registered here. */
export function useWorkspaceContributions() {
  const [pages, setPages] = useState<WorkspaceContribution[]>([]);
  useEffect(() => {
    let disposed = false;
    let unsubscribe: (() => void) | undefined;
    let timer: number | undefined;
    const read = () => {
      if (disposed) return;
      setPages(
        [...(window.__STUDIO_DSH__?.workspacePages?.() || [])].sort(
          (a, b) => (a.order || 0) - (b.order || 0),
        ),
      );
    };
    const bind = () => {
      unsubscribe?.();
      unsubscribe = window.__STUDIO_DSH__?.subscribeWorkspace?.(read);
      read();
    };
    bind();
    const discoverWithoutCore = () => {
      if (disposed || window.__STUDIO_DSH__) return;
      void Promise.all([
        apiFetch("/api/v1/plugins/teams/lifecycle").then(response => response.ok ? response.json() : null),
        apiFetch("/api/v1/plugins/workspace-contributions").then(response => response.ok ? response.json() : null),
      ])
        .then(([teams, contributions]) => {
          const pages: WorkspaceContribution[] = [];
          // A tab represents a usable contribution, so an installed but
          // disabled/unhealthy plugin must not leave a dead navigation entry.
          if (teams?.available && teams?.enabled && teams?.health === "ready") {
            pages.push({ id: "teams", label: "团队", pluginId: "teams", order: 30 });
          }
          if (Array.isArray(contributions?.items)) pages.push(...contributions.items);
          if (disposed || window.__STUDIO_DSH__) return;
          setPages(pages.sort((a, b) => (a.order || 0) - (b.order || 0)));
        })
        .catch(() => undefined);
    };
    discoverWithoutCore();
    timer = window.setInterval(discoverWithoutCore, 1500);
    const onBridgeReady = () => {
      if (timer !== undefined) window.clearInterval(timer);
      timer = undefined;
      bind();
    };
    window.addEventListener("studio:bridge-ready", onBridgeReady);
    window.addEventListener("studio:workspace-changed", read);
    return () => {
      unsubscribe?.();
      disposed = true;
      if (timer !== undefined) window.clearInterval(timer);
      window.removeEventListener("studio:bridge-ready", onBridgeReady);
      window.removeEventListener("studio:workspace-changed", read);
    };
  }, []);
  return pages;
}

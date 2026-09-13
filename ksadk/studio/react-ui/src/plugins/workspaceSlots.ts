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
    let unsubscribe: (() => void) | undefined;
    let timer: number | undefined;
    const read = () =>
      setPages(
        [...(window.__STUDIO_DSH__?.workspacePages?.() || [])].sort(
          (a, b) => (a.order || 0) - (b.order || 0),
        ),
      );
    const bind = () => {
      unsubscribe?.();
      unsubscribe = window.__STUDIO_DSH__?.subscribeWorkspace?.(read);
      read();
    };
    bind();
    const discoverWithoutCore = () => {
      if (window.__STUDIO_DSH__) return;
      void apiFetch("/api/v1/plugins/teams/lifecycle")
        .then(response => response.ok ? response.json() : null)
        .then(state => {
          // A tab represents a usable contribution, so an installed but
          // disabled/unhealthy plugin must not leave a dead navigation entry.
          if (state?.available && state?.enabled && state?.health === "ready") {
            setPages([{ id: "teams", label: "团队", pluginId: "teams", order: 30 }]);
          } else {
            setPages(current => current.filter(page => page.id !== "teams"));
          }
        })
        .catch(() => undefined);
    };
    discoverWithoutCore();
    timer = window.setInterval(discoverWithoutCore, 1500);
    window.addEventListener("studio:bridge-ready", bind);
    window.addEventListener("studio:workspace-changed", read);
    return () => {
      unsubscribe?.();
      if (timer !== undefined) window.clearInterval(timer);
      window.removeEventListener("studio:bridge-ready", bind);
      window.removeEventListener("studio:workspace-changed", read);
    };
  }, []);
  return pages;
}

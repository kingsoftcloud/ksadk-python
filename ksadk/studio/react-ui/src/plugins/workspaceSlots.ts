import { useEffect, useState } from "react";

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
    window.addEventListener("studio:bridge-ready", bind);
    window.addEventListener("studio:workspace-changed", read);
    return () => {
      unsubscribe?.();
      window.removeEventListener("studio:bridge-ready", bind);
      window.removeEventListener("studio:workspace-changed", read);
    };
  }, []);
  return pages;
}

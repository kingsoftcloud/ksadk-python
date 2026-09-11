import { useEffect, useRef, useState } from "react";
import type { WorkspaceContribution } from "./workspaceSlots";
import { TeamsAvailability } from "../pages/TeamsAvailability";

/** Mount/unmount a live contribution in its original DSH context. */
export function PluginWorkspacePage({
  pageId,
  contributions,
}: {
  pageId: string;
  contributions: WorkspaceContribution[];
}) {
  const host = useRef<HTMLDivElement>(null);
  const contribution = contributions.find((page) => page.id === pageId);
  const [error, setError] = useState("");
  const [retry, setRetry] = useState(0);
  useEffect(() => {
    setError("");
    if (!contribution || !host.current) return;
    try {
      return window.__STUDIO_DSH__?.attachWorkspace?.(pageId, host.current, {});
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "插件页面打开失败。");
    }
  }, [pageId, contribution?.pluginId, Boolean(contribution), retry]);
  if (!contribution)
    return pageId === "teams" ? (
      <TeamsAvailability />
    ) : (
      <div className="studio-plugin-empty">
        <h2>此插件工作区尚未加载</h2>
        <p>请在插件管理中启用对应插件。</p>
        <a className="button secondary" href="#/plugins">
          打开插件管理
        </a>
      </div>
    );
  return (
    <div className="studio-plugin-page-surface">
      {error && (
        <div role="alert" className="studio-plugin-empty">
          <p>{error}</p>
          <button
            className="button secondary"
            onClick={() => setRetry((value) => value + 1)}
          >
            重试
          </button>
        </div>
      )}
      <div ref={host} className="studio-plugin-page-mount" />
    </div>
  );
}

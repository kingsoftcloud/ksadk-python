import { useEffect, useState } from "react";
import { UsersRound } from "lucide-react";
import { apiFetch } from "../api";

export type TeamsFailure = { code: string; stage: string; retryable: boolean; recoveryUrl?: string; reason?: string; message?: string };
export type TeamsLifecycle = {
  enabled: boolean;
  mode?: "local" | "server";
  authorityLocation?: "local" | "server";
  configuredEnabled?: boolean | null;
  available?: boolean;
  apiVersion: string;
  health: string;
  authorityRef: string;
  stage?: string;
  reason?: string;
  failure?: TeamsFailure | null;
  recoveryUrl?: string;
};
export class TeamRequestError extends Error {
  constructor(message: string, readonly code?: string, readonly details?: TeamsFailure, readonly status?: number) { super(message); }
}
export async function teamRequest<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await apiFetch(path, init);
  const body = response.status === 204 ? undefined : await response.json();
  if (!response.ok) throw new TeamRequestError(body?.error?.message || body?.detail || `请求失败（${response.status}）`, body?.error?.code, body?.error?.details, response.status);
  return body as T;
}
export const readTeamsLifecycle = (signal?: AbortSignal) => teamRequest<TeamsLifecycle>("/api/v1/plugins/teams/lifecycle", { signal });

/** Recovery never requires a live Teams contribution or a running model. */
export function TeamsAvailability({ compact = false }: { compact?: boolean }) {
  const [state, setState] = useState<TeamsLifecycle | null>(null);
  const [error, setError] = useState("");
  const [failure, setFailure] = useState<TeamsFailure | null>(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    const controller = new AbortController();
    void readTeamsLifecycle(controller.signal).then(setState).catch(cause => { if (!controller.signal.aborted) setError(cause.message); });
    return () => controller.abort();
  }, []);
  const issue = failure || state?.failure;
  const failed = state?.health === "failed" || Boolean(issue);
  async function repair() {
    setBusy(true); setError("");
    try {
      const next = await teamRequest<TeamsLifecycle>("/api/v1/plugins/teams/repair", { method: "POST" });
      setState(next); setFailure(null);
      if (next.enabled) window.location.reload();
    } catch (cause) { setError(cause instanceof Error ? cause.message : "修复未完成，原历史仍保留。"); }
    finally { setBusy(false); }
  }
  async function configure(enabled: boolean) {
    setBusy(true); setError(""); setFailure(null);
    try {
      const next = await teamRequest<TeamsLifecycle>("/api/v1/plugins/teams/lifecycle", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ enabled }) });
      setState(next);
      if (!enabled || next.health !== "ready") return;
      if (window.__STUDIO_DSH__?.workspacePages?.().some(page => page.id === "teams")) { window.location.hash = "/workspace/teams"; window.location.reload(); return; }
      await teamRequest("/api/v1/plugin-ecosystems/dsh/core/session", { method: "POST" });
      window.location.assign("/studio-core/?workspacePage=teams#/workspace/teams");
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "团队插件启用失败。");
      if (cause instanceof TeamRequestError) setFailure(cause.details || null);
    } finally { setBusy(false); }
  }
  return <section className={failed ? "studio-teams-recovery" : compact ? "studio-teams-availability compact" : "studio-teams-availability"} aria-label="Agent Teams 插件">
    {!failed && <span className="studio-teams-symbol"><UsersRound size={23} /></span>}
    <div><span className="studio-teams-eyebrow">Agent Teams</span><h2>{failed ? "团队暂时无法启动" : compact ? "让多个 Agent 一起完成目标" : state?.enabled ? "打开团队工作区" : "组建你的 Agent 团队"}</h2>
      <p role={failed ? "alert" : undefined}>{failed ? error || issue?.message || issue?.reason || state?.reason || "启动没有完成。修复后可以继续使用已有团队与记录。" : "选择成员与 Leader，为不同目标建立独立任务。"}</p>
      {error && !failed && <p className="form-error" role="alert">{error}</p>}
      <div className="studio-teams-recovery-actions"><button className="button primary" disabled={busy || (!state && !error) || (failed && issue?.retryable === false)} onClick={() => void configure(true)}>{busy ? "正在处理…" : failed ? "重新尝试启动" : state?.enabled ? "打开团队" : "启用 Agent Teams"}</button>
        {failed && (state?.configuredEnabled !== false) && <button className="button secondary" disabled={busy} onClick={() => void configure(false)}>暂时禁用</button>}
        {issue?.code === "artifact_migration_required" && <button className="button secondary" disabled={busy} onClick={() => void repair()}>备份并升级兼容历史</button>}
        {failed ? <a href="/studio-recovery/" className="button tertiary">修复与诊断</a> : !compact && <a href="#/agents" className="button tertiary">管理 Agent</a>}
      </div>
      {issue && <details><summary>查看诊断信息</summary><dl><dt>错误</dt><dd>{issue.code}</dd><dt>阶段</dt><dd>{issue.stage || state?.stage}</dd></dl></details>}
    </div>
  </section>;
}

import { useEffect, useState } from "react";
import { UsersRound } from "lucide-react";
import { apiFetch } from "../api";

export type TeamsLifecycle = {
  enabled: boolean;
  apiVersion: string;
  health: string;
  authorityRef: string;
};
export async function teamRequest<T>(
  path: string,
  init: RequestInit = {},
): Promise<T> {
  const response = await apiFetch(path, init);
  const body = response.status === 204 ? undefined : await response.json();
  if (!response.ok)
    throw new Error(
      body?.error?.message || body?.detail || `请求失败（${response.status}）`,
    );
  return body as T;
}
export const readTeamsLifecycle = (signal?: AbortSignal) =>
  teamRequest<TeamsLifecycle>("/api/v1/plugins/teams/lifecycle", { signal });

/** An availability screen is not a live workspace contribution. */
export function TeamsAvailability({ compact = false }: { compact?: boolean }) {
  const [state, setState] = useState<TeamsLifecycle | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    const controller = new AbortController();
    void readTeamsLifecycle(controller.signal)
      .then(setState)
      .catch((cause) => {
        if (!controller.signal.aborted) setError(cause.message);
      });
    return () => controller.abort();
  }, []);
  async function enable() {
    setBusy(true);
    setError("");
    try {
      if (!state?.enabled)
        setState(
          await teamRequest("/api/v1/plugins/teams/lifecycle", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ enabled: true }),
          }),
        );
      if (
        window.__STUDIO_DSH__
          ?.workspacePages?.()
          .some((page) => page.id === "teams")
      ) {
        window.location.hash = "/workspace/teams";
        return;
      }
      await teamRequest("/api/v1/plugin-ecosystems/dsh/core/session", {
        method: "POST",
      });
      window.location.assign(
        "/studio-core/?workspacePage=teams#/workspace/teams",
      );
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "团队插件启用失败。");
    } finally {
      setBusy(false);
    }
  }
  return (
    <section
      className={
        compact
          ? "studio-teams-availability compact"
          : "studio-teams-availability"
      }
      aria-label="Agent Teams 插件"
    >
      <span className="studio-teams-symbol">
        <UsersRound size={23} />
      </span>
      <div>
        <span className="studio-teams-eyebrow">Agent Teams <b className="studio-beta-badge">Beta</b></span>
        <h2>
          {compact
            ? "让多个 Agent 一起完成目标"
            : state?.enabled
              ? "打开团队工作区"
              : "组建你的 Agent 团队"}
        </h2>
        <p>选择成员和 Leader，在群聊中分工、跟进任务并验收成果。</p>
        {error && (
          <p className="form-error" role="alert">
            {error}
          </p>
        )}
        <button
          className="button primary"
          disabled={busy || (!state && !error)}
          onClick={() => void enable()}
        >
          {busy
            ? "正在准备团队…"
            : state?.enabled
              ? "打开团队"
              : "启用 Agent Teams"}
        </button>
        {!compact && (
          <a href="#/agents" className="button tertiary">
            管理 Agent
          </a>
        )}
      </div>
    </section>
  );
}

import { useEffect, useState } from "react";
import { UsersRound } from "lucide-react";
import { apiFetch } from "../api";

export type TeamsLifecycle = {
  enabled: boolean;
  apiVersion: string;
  health: string;
  authorityRef: string;
  reason?: string | null;
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
    let timer: ReturnType<typeof setTimeout> | undefined;
    const read = async () => {
      try {
        const next = await readTeamsLifecycle(controller.signal);
        if (controller.signal.aborted) return;
        setState(next);
        setError("");
      } catch (cause) {
        if (!controller.signal.aborted)
          setError(cause instanceof Error ? cause.message : "团队状态读取失败。");
      } finally {
        if (!controller.signal.aborted) timer = setTimeout(() => void read(), 1500);
      }
    };
    void read();
    return () => { controller.abort(); clearTimeout(timer); };
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
        {!error && state?.health === "error" && (
          <p className="form-error" role="alert">
            {state.reason === "authority_in_use"
              ? "此工作区的团队正在另一个 Studio 中运行。关闭该实例后重试。"
              : state.reason === "DSH_CAPABILITY_HOST_UNAVAILABLE"
              ? "团队插件未能连接本地插件服务，请重试。"
              : "团队插件自动准备未完成，请重试。"}
          </p>
        )}
        {error && (
          <p className="form-error" role="alert">
            {error}
          </p>
        )}
        <button
          className="button primary"
          disabled={busy || state?.health === "preparing" || (!state && !error)}
          onClick={() => void enable()}
        >
          {busy || state?.health === "preparing"
            ? "正在准备团队…"
            : state?.enabled
              ? "打开团队"
              : state?.health === "error" ? "重试准备团队" : "启用 Agent Teams"}
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

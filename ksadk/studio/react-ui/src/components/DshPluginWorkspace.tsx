import { useEffect, useState } from "react";
import { ArrowLeft, LoaderCircle, RefreshCw } from "lucide-react";
import { apiFetch } from "../api";

/** Render the official Core client; Studio does not emulate its plugin APIs. */
export function DshPluginWorkspace({ onBack }: { onBack: () => void }) {
  const [attempt, setAttempt] = useState(0);
  const [browserUrl, setBrowserUrl] = useState("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setError("");
    setBrowserUrl("");
    void (async () => {
      try {
        const response = await apiFetch("/api/v1/plugin-ecosystems/dsh/core/session", {
          method: "POST", signal: controller.signal,
        });
        const payload = await response.json();
        if (!response.ok) throw new Error(payload?.error?.message || "插件工作台启动失败");
        const url = new URL(payload.browserUrl);
        if (url.protocol !== "http:" || url.hostname !== "127.0.0.1" || !url.port || url.username || url.password) {
          throw new Error("插件工作台地址无效");
        }
        // Ephemeral handoff only: never persist the token in location or storage.
        if (!controller.signal.aborted) setBrowserUrl(url.href);
      } catch (cause) {
        if (controller.signal.aborted) return;
        setError(cause instanceof Error ? cause.message : "插件工作台启动失败");
        setLoading(false);
      }
    })();
    return () => controller.abort();
  }, [attempt]);

  return <section className="dsh-plugin-workspace" aria-label="DSH 插件工作台">
    <header>
      <button className="button secondary" onClick={onBack}><ArrowLeft size={16}/>返回插件列表</button>
      <span>DSH 插件工作台</span>
      <button className="button secondary" disabled={loading} onClick={() => setAttempt(value => value + 1)}><RefreshCw size={16}/>{error ? "重试" : "重新加载"}</button>
    </header>
    {loading && <p className="dsh-workspace-status" role="status"><LoaderCircle className="animate-spin" size={18}/>正在打开插件工作台…</p>}
    {error && <p className="form-error" role="alert">{error}</p>}
    {browserUrl && <iframe
      title="DSH 插件工作台"
      src={browserUrl}
      referrerPolicy="no-referrer"
      onLoad={() => setLoading(false)}
      onError={() => { setLoading(false); setError("插件工作台加载失败，请重试"); }}
    />}
  </section>;
}

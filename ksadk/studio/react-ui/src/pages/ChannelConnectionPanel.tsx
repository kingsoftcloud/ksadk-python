import { useEffect, useState } from "react";
import { apiFetch } from "../api";

type Agent = { metadata: { id: string; name: string } };
type Connection = {
  serverUrl: string; accountId: string; workspaceId: string; hasApiToken: boolean;
  agents: Array<{ agentId: string; enabled: boolean; hasToken: boolean; connected: boolean; state: string }>;
};

async function connectionRequest(path: string, body?: unknown): Promise<Connection> {
  const response = await apiFetch(path, body === undefined ? {} : {
    method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error?.message || "连接配置失败，请检查服务地址与凭证");
  return data;
}

export function ChannelConnectionPanel({ agents, onWorkspace, onChange }: {
  agents: Agent[]; onWorkspace: (id: string) => void; onChange: () => void;
}) {
  const [status, setStatus] = useState<Connection | null>(null);
  const [form, setForm] = useState({ serverUrl: "", accountId: "", workspaceId: "", apiToken: "" });
  const [agentId, setAgentId] = useState("");
  const [token, setToken] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    let disposed = false;
    let initialized = false;
    async function refresh() {
      try {
        const data = await connectionRequest("/api/v1/channels/connection");
        if (disposed) return;
        setStatus(data);
        onWorkspace(data.workspaceId);
        if (!initialized) {
          setForm({ serverUrl: data.serverUrl, accountId: data.accountId, workspaceId: data.workspaceId, apiToken: "" });
          initialized = true;
        }
      } catch (failure) {
        if (!disposed) setError(failure instanceof Error ? failure.message : "连接状态暂不可用");
      }
    }
    void refresh();
    const timer = window.setInterval(() => void refresh(), 3000);
    return () => { disposed = true; window.clearInterval(timer); };
  }, [onWorkspace]);

  async function saveService(event: React.FormEvent) {
    event.preventDefault(); setBusy(true); setError("");
    try {
      const { apiToken, ...config } = form;
      const data = await connectionRequest("/api/v1/channels/connection", { ...config, ...(apiToken ? { apiToken } : {}) });
      setStatus(data); setForm(value => ({ ...value, workspaceId: data.workspaceId, apiToken: "" }));
      onWorkspace(data.workspaceId); onChange();
    } catch (failure) { setError(failure instanceof Error ? failure.message : "保存失败"); }
    finally { setBusy(false); }
  }

  async function setAgentEnabled(enabled: boolean) {
    setBusy(true); setError("");
    try {
      const data = await connectionRequest(`/api/v1/channels/agents/${encodeURIComponent(agentId)}/connection`, { enabled, ...(token ? { token } : {}) });
      setStatus(data); setToken(""); onChange();
    } catch (failure) { setError(failure instanceof Error ? failure.message : "Agent 连接失败"); }
    finally { setBusy(false); }
  }

  const selected = status?.agents.find(agent => agent.agentId === agentId);
  const online = status?.agents.filter(agent => agent.connected).length || 0;
  return <details className="channels-connection" open={!status?.serverUrl}>
    <summary>渠道服务连接 · {online ? `${online} 个本地 Agent 在线` : status?.serverUrl ? "暂无本地 Agent 在线" : "尚未配置"}</summary>
    {error && <p role="alert">{error}</p>}
    <form className="channels-connection__form" onSubmit={saveService}>
      <label>服务地址<input type="url" value={form.serverUrl} placeholder="https://channel.example.com" onChange={event => setForm({ ...form, serverUrl: event.target.value })} required /></label>
      <label>账号 ID<input value={form.accountId} onChange={event => setForm({ ...form, accountId: event.target.value })} required /></label>
      <label>工作区 ID<input value={form.workspaceId} onChange={event => setForm({ ...form, workspaceId: event.target.value })} required /></label>
      <label>服务访问凭证<input type="password" autoComplete="new-password" value={form.apiToken} placeholder={status?.hasApiToken ? "已保存，留空保留" : "按服务入口要求填写"} onChange={event => setForm({ ...form, apiToken: event.target.value })} /></label>
      <button className="button secondary" disabled={busy}>保存连接配置</button>
    </form>
    <div className="channels-connection__form">
      <label>本地 Agent<select value={agentId} onChange={event => { setAgentId(event.target.value); setToken(""); }}>
        <option value="">选择需要接收消息的 Agent</option>
        {agents.map(agent => <option key={agent.metadata.id} value={agent.metadata.id}>{agent.metadata.name}</option>)}
      </select></label>
      <label>Agent 连接凭证<input type="password" autoComplete="new-password" value={token} placeholder={selected?.hasToken ? "已保存，留空保留" : "由渠道服务签发"} onChange={event => setToken(event.target.value)} /></label>
      <button className="button secondary" disabled={busy || !agentId || !status?.serverUrl} onClick={() => void setAgentEnabled(true)}>连接 Agent</button>
      <button className="button tertiary" disabled={busy || !selected?.enabled} onClick={() => void setAgentEnabled(false)}>断开</button>
      {agentId && <span role="status">{selected?.connected ? "已连接" : selected?.enabled ? "正在连接或等待重连" : "未连接"}</span>}
    </div>
    <p>本地 Agent 会在 Studio 运行期间接收渠道消息。关闭页面不影响连接，退出 Studio 后离线。</p>
  </details>;
}

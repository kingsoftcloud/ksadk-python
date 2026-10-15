import { useEffect, useId, useState } from "react";
import { apiFetch } from "../api";

export interface RemoteAgentBinding {
  bindingId: string;
  spaceId: string;
  targetAgentId: string;
  expectedVersionId: string;
  localName: string;
  exposure: "native" | "tool" | "auto";
  policyRef: string;
  digest: string;
}
type Space = { spaceId: string; name: string; status: string };
type Agent = { targetAgentId: string; name: string; versionId: string };

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await apiFetch(`/api/v1/a2a/${path}`, init);
  if (!response.ok) {
    // Do not render arbitrary upstream error text or transport details.
    throw new Error(response.status === 409 || response.status === 422
      ? "绑定已变化或所选成员不可调用，请重新选择。"
      : "无法访问 A2A 目录，请检查云端连接后重试。");
  }
  return response.json();
}

export function RemoteAgentBindingsEditor({ value, onChange }: {
  value: RemoteAgentBinding[];
  onChange: (value: RemoteAgentBinding[]) => void;
}) {
  const id = useId();
  const [opened, setOpened] = useState(false);
  const [spaces, setSpaces] = useState<Space[]>([]);
  const [page, setPage] = useState(1);
  const [total, setTotal] = useState(0);
  const [spaceId, setSpaceId] = useState("");
  const [agents, setAgents] = useState<Agent[]>([]);
  const [targetId, setTargetId] = useState("");
  const callableVersion = agents.find(agent => agent.targetAgentId === targetId)?.versionId;
  const [versionId, setVersionId] = useState("");
  const [localName, setLocalName] = useState("");
  const [policyRef, setPolicyRef] = useState("policy://a2a/runtime");
  const [exposure, setExposure] = useState<RemoteAgentBinding["exposure"]>("auto");
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [retry, setRetry] = useState(0);

  useEffect(() => {
    if (!opened) return;
    let active = true;
    setLoading(true);
    setError("");
    request<{ items: Space[]; total: number }>(`spaces?page=${page}&size=20`)
      .then(data => { if (active) { setSpaces(data.items); setTotal(data.total); } })
      .catch(err => { if (active) setError(err.message); })
      .finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [opened, page, retry]);

  useEffect(() => {
    if (!spaceId) return;
    let active = true;
    setError("");
    request<{ items: Agent[] }>(`spaces/${encodeURIComponent(spaceId)}/agents`)
      .then(data => { if (active) setAgents(data.items); })
      .catch(err => { if (active) setError(err.message); });
    return () => { active = false; };
  }, [spaceId, retry]);

  async function freeze() {
    if (busy) return;
    if (value.some(row => row.localName === localName || row.targetAgentId === targetId)) {
      setError("调用名称或目标 Agent 已绑定，请先移除原绑定。");
      return;
    }
    setBusy(true);
    setError("");
    try {
      const binding = await request<RemoteAgentBinding>("bindings:freeze", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ spaceId, targetAgentId: targetId, expectedVersionId: versionId,
          localName, exposure, policyRef }),
      });
      onChange([...value, binding]);
      setLocalName("");
    } catch (err) {
      setError(err instanceof Error ? err.message : "绑定失败，请重新选择。");
    } finally { setBusy(false); }
  }

  return <details className="subagent-bindings" onToggle={event => setOpened(event.currentTarget.open)}>
    <summary>远端 A2A Agent <span className="helper">{value.length} 个已绑定</span></summary>
    <p className="agent-policy-note">选择空间内可调用的 Agent 并固定版本。成员或版本变化后，构建与部署会要求重新绑定。</p>
    {value.map(row => <fieldset className="subagent-binding" key={row.bindingId}>
      <legend>{row.localName}</legend>
      <p>空间：{row.spaceId} · Agent：{row.targetAgentId}</p>
      <p>固定版本：{row.expectedVersionId} · 调用方式：{row.exposure}</p>
      <p>策略：{row.policyRef}</p>
      <button type="button" disabled={busy} onClick={() => onChange(value.filter(item => item.bindingId !== row.bindingId))}>移除 {row.localName}</button>
    </fieldset>)}
    <fieldset disabled={busy}>
      <legend>添加远端绑定</legend>
      <label className="field" htmlFor={`${id}-space`}>A2A Space<select id={`${id}-space`} value={spaceId} disabled={loading} onChange={event => {
        setSpaceId(event.target.value); setTargetId(""); setVersionId(""); setAgents([]);
      }}><option value="">选择空间</option>{spaces.map(item => <option key={item.spaceId} value={item.spaceId} disabled={item.status !== "available"}>{item.name} ({item.spaceId})</option>)}</select></label>
      <button type="button" disabled={loading || page <= 1} onClick={() => setPage(page - 1)}>上一页空间</button>
      <button type="button" disabled={loading || page * 20 >= total} onClick={() => setPage(page + 1)}>下一页空间</button>
      <label className="field" htmlFor={`${id}-agent`}>远端 Agent<select id={`${id}-agent`} value={targetId} onChange={event => {
        setTargetId(event.target.value); setVersionId("");
      }}><option value="">选择 Agent</option>{agents.map(item => <option key={item.targetAgentId} value={item.targetAgentId}>{item.name} ({item.targetAgentId})</option>)}</select></label>
      <label className="field" htmlFor={`${id}-version`}>固定版本<select id={`${id}-version`} value={versionId} onChange={event => setVersionId(event.target.value)}>
        <option value="">选择版本</option>{callableVersion && <option value={callableVersion}>当前可调用版本 ({callableVersion})</option>}
      </select></label>
      <p className="helper">固定当前可调用版本；服务版本变化后须重新确认绑定。</p>
      <label className="field" htmlFor={`${id}-name`}>调用名称<input id={`${id}-name`} value={localName} maxLength={128} onChange={event => setLocalName(event.target.value)} /></label>
      <label className="field" htmlFor={`${id}-policy`}>策略引用<select id={`${id}-policy`} value={policyRef} onChange={event => setPolicyRef(event.target.value)}><option value="policy://a2a/runtime">遵循当前会话审批设置</option><option value="policy://a2a/strict">每次调用均需审批</option></select></label>
      <label className="field" htmlFor={`${id}-exposure`}>调用方式<select id={`${id}-exposure`} value={exposure} onChange={event => setExposure(event.target.value as RemoteAgentBinding["exposure"])}><option value="auto">自动</option><option value="native">原生节点</option><option value="tool">工具</option></select></label>
      <button type="button" disabled={!spaceId || !targetId || !versionId || versionId !== callableVersion || !/^[A-Za-z_][A-Za-z0-9_]*$/.test(localName) || !["policy://a2a/runtime", "policy://a2a/strict"].includes(policyRef)} onClick={freeze}>{busy ? "正在固定版本…" : "固定此绑定"}</button>
    </fieldset>
    {error && <p role="alert">{error}</p>}
    <button type="button" disabled={busy || loading} onClick={() => { setSpaceId(""); setAgents([]); setTargetId(""); setVersionId(""); setRetry(retry + 1); }}>刷新目录</button>
  </details>;
}

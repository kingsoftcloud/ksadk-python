import { useEffect, useId, useRef, useState } from "react";
import { apiFetch } from "../api";
import { CodeViewer } from "./ui/CodeViewer";
import type { RemoteAgentBinding } from "./RemoteAgentBindingsEditor";

export interface A2ADesignNode { nodeId: string; kind: "local" | "remote"; bindingId?: string }
export interface A2ADesignEdge { source: string; target: string; kind: "sequential" | "handoff" }
export interface A2AOrchestration { entryNodeId: string; nodes: A2ADesignNode[]; edges: A2ADesignEdge[] }
interface Preview { plan: { sequence: string[]; handoffs: { source: string; target: string }[] }; digest: string; source: string; entrySource: string }
interface Runtime { type: string; projectPath: string; entryPoint: string; agentVariable: string }

export function A2AOrchestrationEditor({ agentId, revision, runtime, bindings, value, onChange }: {
  agentId: string; revision: number; runtime: Runtime; bindings: RemoteAgentBinding[];
  value: A2AOrchestration | null; onChange: (value: A2AOrchestration | null) => void;
}) {
  const id = useId();
  const [preview, setPreview] = useState<Preview | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const fingerprint = JSON.stringify({ agentId, revision, runtime, bindings, value });
  const current = useRef(fingerprint);
  current.current = fingerprint;
  useEffect(() => { setPreview(null); setError(""); }, [fingerprint]);
  const supported = ["adk", "langgraph"].includes(runtime.type);

  function update(next: A2AOrchestration) { onChange(next); }
  function addNode(kind: "local" | "remote") {
    if (!value) return;
    let suffix = 1;
    while (value.nodes.some(node => node.nodeId === `${kind}_${suffix}`)) suffix++;
    const binding = bindings.find(row => !value.nodes.some(node => node.bindingId === row.bindingId));
    if (kind === "remote" && !binding) return;
    update({ ...value, nodes: [...value.nodes, { nodeId: `${kind}_${suffix}`, kind, ...(kind === "remote" ? { bindingId: binding!.bindingId } : {}) }] });
  }
  function rename(previous: string, next: string) {
    if (!value) return;
    update({ entryNodeId: value.entryNodeId === previous ? next : value.entryNodeId,
      nodes: value.nodes.map(node => node.nodeId === previous ? { ...node, nodeId: next } : node),
      edges: value.edges.map(edge => ({ ...edge, source: edge.source === previous ? next : edge.source, target: edge.target === previous ? next : edge.target })) });
  }
  async function compilePreview() {
    if (!value || busy) return;
    const requested = fingerprint;
    setBusy(true); setPreview(null); setError("");
    try {
      const response = await apiFetch(`/api/v1/agents/${encodeURIComponent(agentId)}/a2a:preview`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ expectedRevision: revision, runtime, bindings, orchestration: value }),
      });
      if (current.current !== requested) return;
      if (!response.ok) {
        setError(response.status === 409
          ? "Agent、空间或目标版本已变化，请刷新目录并重新绑定。"
          : "编排校验失败：请检查节点名称、绑定及连线；仅支持顺序单链和本地节点到远端的 handoff。");
        return;
      }
      const result = await response.json();
      if (current.current === requested) setPreview(result);
    } catch {
      if (current.current === requested) setError("暂时无法预览，请检查 Studio 连接后重试。");
    } finally { setBusy(false); }
  }
  return <details className="subagent-bindings">
    <summary>A2A 编排 <span className="helper">{value ? `${value.nodes.length} 个节点` : "未启用"}</span></summary>
    <p className="agent-policy-note">顺序节点依次执行；handoff 由本地 Agent 按需调用远端工具并接收结果。多个本地节点需要项目提供对应的节点映射。</p>
    {!supported && <p role="status">当前 Runtime 的编排尚未开放。</p>}
    {!value ? <button type="button" disabled={!supported} onClick={() => onChange({ entryNodeId: "local", nodes: [{ nodeId: "local", kind: "local" }], edges: [] })}>启用 A2A 编排</button> : <>
      <fieldset disabled={!supported || busy}>
        <legend>节点与连线</legend>
        {value.nodes.map((node, index) => <fieldset key={index} className="subagent-binding">
          <legend>{node.kind === "local" ? "本地节点" : "远端节点"} {index + 1}</legend>
          <label className="field" htmlFor={`${id}-node-${index}`}>节点名称 {index + 1}<input id={`${id}-node-${index}`} value={node.nodeId} maxLength={128} onChange={event => rename(node.nodeId, event.target.value)} /></label>
          {node.kind === "remote" && <label className="field" htmlFor={`${id}-binding-${index}`}>远端绑定 {index + 1}<select id={`${id}-binding-${index}`} value={node.bindingId || ""} onChange={event => update({ ...value, nodes: value.nodes.map((row, i) => i === index ? { ...row, bindingId: event.target.value } : row) })}>
            {!bindings.some(binding => binding.bindingId === node.bindingId) && <option value={node.bindingId}>绑定已移除，请重新选择</option>}
            {bindings.map(binding => <option key={binding.bindingId} value={binding.bindingId}>{binding.localName} · {binding.expectedVersionId}</option>)}
          </select></label>}
          <button type="button" onClick={() => update({ ...value, entryNodeId: value.entryNodeId === node.nodeId ? "" : value.entryNodeId, nodes: value.nodes.filter((_, i) => i !== index), edges: value.edges.filter(edge => edge.source !== node.nodeId && edge.target !== node.nodeId) })}>移除节点 {index + 1}</button>
        </fieldset>)}
        <button type="button" disabled={value.nodes.length >= 64} onClick={() => addNode("local")}>添加本地节点</button>
        <button type="button" disabled={value.nodes.length >= 64 || !bindings.some(row => !value.nodes.some(node => node.bindingId === row.bindingId))} onClick={() => addNode("remote")}>添加远端节点</button>
        <label className="field" htmlFor={`${id}-entry`}>入口节点<select id={`${id}-entry`} value={value.entryNodeId} onChange={event => update({ ...value, entryNodeId: event.target.value })}><option value="">选择入口</option>{value.nodes.map((node, index) => <option key={index} value={node.nodeId}>{node.nodeId}</option>)}</select></label>
        {value.edges.map((edge, index) => <fieldset key={index}>
          <legend>连线 {index + 1}</legend>
          {(["source", "target"] as const).map(side => <label key={side} className="field" htmlFor={`${id}-${side}-${index}`}>{side === "source" ? "起点" : "终点"} {index + 1}<select id={`${id}-${side}-${index}`} value={edge[side]} onChange={event => update({ ...value, edges: value.edges.map((row, i) => i === index ? { ...row, [side]: event.target.value } : row) })}>
            <option value="">选择节点</option>{value.nodes.map((node, i) => <option key={i} value={node.nodeId}>{node.nodeId}</option>)}
          </select></label>)}
          <label className="field" htmlFor={`${id}-kind-${index}`}>连线方式 {index + 1}<select id={`${id}-kind-${index}`} value={edge.kind} onChange={event => update({ ...value, edges: value.edges.map((row, i) => i === index ? { ...row, kind: event.target.value as A2ADesignEdge["kind"] } : row) })}><option value="sequential">顺序</option><option value="handoff">Handoff 工具</option></select></label>
          <button type="button" onClick={() => update({ ...value, edges: value.edges.filter((_, i) => i !== index) })}>移除连线 {index + 1}</button>
        </fieldset>)}
        <button type="button" disabled={value.edges.length >= 63 || value.nodes.length < 2} onClick={() => update({ ...value, edges: [...value.edges, { source: "", target: "", kind: "sequential" }] })}>添加连线</button>
        <button type="button" onClick={compilePreview}>预览当前编排</button>
      </fieldset>
      <button type="button" disabled={busy} onClick={() => onChange(null)}>移除编排</button>
      {busy && <p role="status">正在校验绑定并编译…</p>}
      {error && <p role="alert">{error}</p>}
      {preview && <section aria-label="编排预览">
        <p>顺序：{preview.plan.sequence.join(" → ")}</p>
        {preview.plan.handoffs.map(edge => <p key={`${edge.source}:${edge.target}`}>Handoff：{edge.source} → {edge.target}</p>)}
        <p>摘要：{preview.digest}</p><p className="helper">此预览尚未保存。保存 Agent 后再构建；构建时会重新核验绑定。</p>
        <CodeViewer filename="studio_a2a_composition.py" language="python" code={preview.source} />
        <CodeViewer filename="studio_a2a_entry.py" language="python" code={preview.entrySource} />
      </section>}
    </>}
  </details>;
}

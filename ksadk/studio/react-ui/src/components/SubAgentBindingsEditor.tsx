import { useId } from "react";

export type SubAgentBinding = {
  name: string;
  instructions: string;
  description?: string;
  tools?: string[];
  maxTurns?: number;
  timeoutSeconds?: number;
  inheritSkills?: boolean;
  inheritMcp?: boolean;
  [key: string]: unknown;
};
export function validateSubAgentBindings(rows: SubAgentBinding[], runtime: string): string | null {
  if (rows.length && runtime !== "harness") return "子 Agent 当前需要 Harness Runtime，请先移除子 Agent 或切回 Harness。";
  if (new Set(rows.map(row => row.name)).size !== rows.length) return "子 Agent 名称不能重复。";
  if (rows.some(row => !/^[a-z0-9][a-z0-9_-]{0,63}$/.test(row.name) || !row.instructions.trim())) return "请填写子 Agent 的名称和提示词；名称使用小写字母、数字、短横线或下划线。";
  if (rows.some(row => !Number.isInteger(row.maxTurns ?? 4) || (row.maxTurns ?? 4) < 1 || (row.maxTurns ?? 4) > 32 || !(Number(row.timeoutSeconds ?? 120) > 0) || Number(row.timeoutSeconds ?? 120) > 3600)) return "子 Agent 的模型轮数应为 1–32，超时应为 1–3600 秒。";
  return null;
}
export function SubAgentBindingsEditor({ value, tools, onChange }: {
  value: SubAgentBinding[];
  tools: { name: string; label: string }[];
  onChange: (value: SubAgentBinding[]) => void;
}) {
  const id = useId();
  const update = (index: number, patch: Partial<SubAgentBinding>) => onChange(value.map((row, position) => position === index ? { ...row, ...patch } : row));
  function add() {
    let suffix = value.length + 1;
    while (value.some(row => row.name === `helper_${suffix}`)) suffix++;
    onChange([...value, { name: `helper_${suffix}`, description: "", instructions: "", tools: [], maxTurns: 4, timeoutSeconds: 120, inheritSkills: false, inheritMcp: false }]);
  }
  return <details className="subagent-bindings">
    <summary>子 Agent <span className="helper">{value.length ? `${value.length} 个已配置` : "可选"}</span></summary>
    <p className="agent-policy-note">由当前 Agent 按需委派独立子任务；执行过程可在团队的执行调用中查看。</p>
    {value.map((row, index) => <fieldset className="subagent-binding" key={index}>
      <legend>子 Agent {index + 1}</legend>
      <div className="form-grid two-columns">
        <label className="field" htmlFor={`${id}-${index}-name`}>调用名称<input id={`${id}-${index}-name`} value={row.name} maxLength={64} onChange={event => update(index, { name: event.target.value })} /></label>
        <label className="field" htmlFor={`${id}-${index}-description`}>用途说明<input id={`${id}-${index}-description`} value={row.description || ""} maxLength={1024} onChange={event => update(index, { description: event.target.value })} /></label>
      </div>
      <label className="field" htmlFor={`${id}-${index}-instructions`}>子 Agent 提示词<textarea id={`${id}-${index}-instructions`} rows={4} value={row.instructions} maxLength={32768} onChange={event => update(index, { instructions: event.target.value })} /></label>
      <div className="form-grid two-columns">
        <label className="field" htmlFor={`${id}-${index}-turns`}>最大模型轮数<input id={`${id}-${index}-turns`} type="number" min={1} max={32} value={row.maxTurns ?? 4} onChange={event => update(index, { maxTurns: Number(event.target.value) })} /></label>
        <label className="field" htmlFor={`${id}-${index}-timeout`}>超时（秒）<input id={`${id}-${index}-timeout`} type="number" min={1} max={3600} value={row.timeoutSeconds ?? 120} onChange={event => update(index, { timeoutSeconds: Number(event.target.value) })} /></label>
      </div>
      <fieldset className="subagent-tools"><legend>允许使用的父 Agent 工具</legend>
        {tools.map(tool => <label key={tool.name}><input type="checkbox" checked={(row.tools || []).includes(tool.name)} onChange={event => update(index, { tools: event.target.checked ? [...(row.tools || []), tool.name] : (row.tools || []).filter(name => name !== tool.name) })} />{tool.label}</label>)}
        {!tools.length && <p className="helper">父 Agent 尚未选择工具，子 Agent 将仅进行文本推理。</p>}
        {(row.tools || []).filter(name => !tools.some(tool => tool.name === name)).map(name => <label key={name}><input type="checkbox" checked onChange={() => update(index, { tools: (row.tools || []).filter(item => item !== name) })} />{name}（历史绑定，构建时核验）</label>)}
      </fieldset>
      <div className="subagent-inheritance"><label><input type="checkbox" checked={row.inheritSkills ?? true} onChange={event => update(index, { inheritSkills: event.target.checked })} />继承父 Agent 的 Skill</label><label><input type="checkbox" checked={row.inheritMcp ?? false} onChange={event => update(index, { inheritMcp: event.target.checked })} />继承父 Agent 的 MCP</label></div>
      <button type="button" className="ghost-button" onClick={() => onChange(value.filter((_, position) => position !== index))}>移除此子 Agent</button>
    </fieldset>)}
    <button type="button" className="ghost-button" disabled={value.length >= 32} onClick={add}>添加子 Agent</button>
  </details>;
}

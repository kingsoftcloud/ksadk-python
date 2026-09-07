import { useEffect, useRef, useState } from "react";
import { apiFetch } from "../api";
import { StudioMultiSelect } from "./ui/StudioMultiSelect";

export interface NativePluginBinding {
  ecosystem: "codex" | "dsh";
  pluginRef: string;
  snapshotDigest: string;
  components: string[];
  enabled: boolean;
  config: Record<string, unknown>;
}
interface Snapshot {
  pluginRef: string;
  snapshotDigest: string;
  components: Array<{ id: string; kind: string }>;
}
interface PluginChoice {
  pluginId: string;
  displayName: string;
  marketplaceName?: string;
  snapshot?: Snapshot;
}
async function read(response: Response) {
  const data = await response.json();
  if (!response.ok || data.error) throw new Error(data.error?.message || "插件加载失败");
  return data;
}

/** Bind admitted plugin snapshots, never the editor host's mutable environment. */
export function NativePluginBindings({ value, onChange, onPendingChange }: {
  value: NativePluginBinding[];
  onChange: (value: NativePluginBinding[]) => void;
  onPendingChange: (pending: boolean) => void;
}) {
  const [choices, setChoices] = useState<PluginChoice[]>([]);
  const [loading, setLoading] = useState(true);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState("");
  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    const controller = new AbortController();
    void (async () => {
      try {
        const inventory = await read(await apiFetch("/api/v1/plugin-ecosystems/codex/plugins?installed_only=true", { signal: controller.signal }));
        const installed = (inventory.items || []).filter((item: { installed: boolean; enabled: boolean }) => item.installed && item.enabled);
        const results = await Promise.allSettled(installed.map(async (item: PluginChoice) => {
          const detail = await read(await apiFetch(`/api/v1/plugin-ecosystems/codex/plugins/${encodeURIComponent(item.pluginId)}`, { signal: controller.signal }));
          return { ...item, snapshot: detail.snapshot || undefined } as PluginChoice;
        }));
        if (controller.signal.aborted) return;
        setChoices(results.flatMap(result => result.status === "fulfilled" ? [result.value as PluginChoice] : []));
        if (results.some(result => result.status === "rejected")) setError("部分插件未能加载；已有绑定保持不变，请稍后重新打开此分区。");
      } catch (reason) {
        if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : "插件加载失败");
      } finally {
        if (!controller.signal.aborted) setLoading(false);
      }
    })();
    return () => { alive.current = false; controller.abort(); };
  }, []);

  // Preserve pinned bindings even when a plugin was removed from the editor host.
  const items = [
    ...choices.map(item => ({ ...item, key: item.snapshot?.pluginRef || item.pluginId })),
    ...value.filter(binding => !choices.some(item => item.snapshot?.pluginRef === binding.pluginRef))
      .map(binding => ({ key: binding.pluginRef, pluginId: binding.pluginRef, displayName: binding.pluginRef, snapshot: undefined })),
  ];
  async function change(ids: string[]) {
    if (pending) return;
    const added = ids.filter(id => !value.some(binding => binding.enabled && binding.pluginRef === id));
    setPending(true);
    onPendingChange(true);
    setError("");
    try {
      const next = value.filter(binding => (!binding.enabled && !added.includes(binding.pluginRef)) || (binding.enabled && ids.includes(binding.pluginRef)));
      for (const id of added) {
        const item = items.find(candidate => candidate.key === id);
        if (!item) throw new Error("插件已不可用，请刷新后重试");
        const snapshot: Snapshot = item.snapshot || (await read(await apiFetch(
          `/api/v1/plugin-ecosystems/codex/plugins/${encodeURIComponent(item.pluginId)}:snapshot`,
          { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ marketplaceName: "marketplaceName" in item ? item.marketplaceName : undefined }) },
        ))).snapshot;
        const components = snapshot.components.filter(component => component.kind !== "hook").map(component => component.id);
        if (!components.length) throw new Error("此插件没有当前 Runtime 支持的组件");
        const previous = value.find(binding => binding.pluginRef === snapshot.pluginRef);
        next.push(previous ? { ...previous, enabled: true } : { ecosystem: "codex", pluginRef: snapshot.pluginRef, snapshotDigest: snapshot.snapshotDigest, components, enabled: true, config: {} });
        if (alive.current) setChoices(current => current.map(choice => choice.pluginId === item.pluginId ? { ...choice, snapshot } : choice));
      }
      if (alive.current) onChange(next);
    } catch (reason) {
      if (alive.current) setError(reason instanceof Error ? reason.message : "插件绑定失败");
    } finally {
      if (alive.current) setPending(false);
      onPendingChange(false);
    }
  }
  return <div className="field quick-model-binding-field">
    <div className="field-heading"><label>绑定插件</label><span className="helper">选择此 Agent 使用的已安装插件。保存并生成配置快照后，在新会话中生效。</span></div>
    {loading ? <p role="status">正在加载已安装插件…</p> : <StudioMultiSelect
      ariaLabel="选择绑定插件" items={items} selectedIds={value.filter(binding => binding.enabled).map(binding => binding.pluginRef)}
      getId={item => item.key} getLabel={item => item.displayName}
      getDescription={item => item.snapshot ? `${item.snapshot.components.filter(component => component.kind !== "hook").length} 项能力` : "绑定时保存插件快照"}
      onChange={ids => void change(ids)} disabledIds={pending ? items.map(item => item.key) : []}
      searchPlaceholder="搜索插件" emptyMessage="没有已启用的插件，请先在插件页安装"
    />}
    {pending && <p role="status">正在准备插件…</p>}
    {error && <p className="inline-alert error" role="alert">{error}</p>}
  </div>;
}

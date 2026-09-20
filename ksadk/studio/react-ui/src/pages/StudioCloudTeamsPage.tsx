import { useCallback, useEffect, useRef, useState, useSyncExternalStore } from "react";
import { ArrowLeft, Plus, Search, UsersRound } from "lucide-react";
import { TEAMS_API_VERSION, CloudOperations, HttpCloudTeamsProductClient, HttpCloudWorkspaceClient, type CloudDirectory, type TeamsOperationPayload } from "@kingsoftcloud/ksadk-web/teams";
import { CloudTeamWorkspace, CreateGroupDialog, type TeamMemberCandidate } from "@kingsoftcloud/ksadk-web/teams/components";
import { apiFetch } from "../api";
import { type TeamsLifecycle } from "./TeamsAvailability";

function selection() { return new URLSearchParams(window.location.hash.split("?")[1] || ""); }
function writeSelection(groupId: string, teamRunId?: string | null) {
  const params = new URLSearchParams(); if (groupId) params.set("groupId", groupId); if (teamRunId) params.set("teamRunId", teamRunId);
  window.history.replaceState(null, "", `#/workspace/teams?${params}`);
}
const messageOf = (cause: unknown) => cause instanceof Error ? cause.message : "读取失败，请重试。";

/** Server mode never falls back to the local full-history reducer. */
export function StudioCloudTeamsPage({ lifecycle, onRetry }: { lifecycle: TeamsLifecycle; onRetry: () => void }) {
  if (lifecycle.apiVersion !== TEAMS_API_VERSION || typeof lifecycle.authorityId !== "string" || !lifecycle.authorityId || typeof lifecycle.ownerScopeRef !== "string" || !lifecycle.ownerScopeRef || !(Array.isArray(lifecycle.features) && lifecycle.features.includes("workspace-projection.v1"))) {
    return <section className="studio-plugin-empty" aria-label="云端团队初始化"><h2>云端团队正在准备</h2><p role="status">{lifecycle.reason || "服务尚未开放分页工作区。历史与执行状态将在服务准备好后显示。"}</p><p>当前状态：{lifecycle.health}</p><button className="button secondary" onClick={onRetry}>刷新服务状态</button><a href="#/plugins">查看插件配置</a></section>;
  }
  return <CloudTeamsBrowser key={`${window.location.origin}:${lifecycle.ownerScopeRef}:${lifecycle.authorityId}`} lifecycle={lifecycle} authorityId={lifecycle.authorityId} ownerScopeRef={lifecycle.ownerScopeRef} onRetry={onRetry} />;
}

function CloudTeamsBrowser({ lifecycle, authorityId, ownerScopeRef, onRetry }: { lifecycle: TeamsLifecycle; authorityId: string; ownerScopeRef: string; onRetry: () => void }) {
  const [product] = useState(() => new HttpCloudTeamsProductClient({ origin: window.location.origin, fetch: apiFetch }));
  const [transport] = useState(() => new HttpCloudWorkspaceClient({ origin: window.location.origin, fetch: apiFetch }));
  const [groups, setGroups] = useState<CloudDirectory["items"]>([]);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [selected, setSelected] = useState(() => selection().get("groupId") || "");
  const [query, setQuery] = useState(""); const [error, setError] = useState(""); const [loading, setLoading] = useState(false);
  const [createOpen, setCreateOpen] = useState(false); const [candidates, setCandidates] = useState<TeamMemberCandidate[]>([]); const [bindingsLoading, setBindingsLoading] = useState(false); const [bindingsError, setBindingsError] = useState(""); const [bindingsCursor, setBindingsCursor] = useState<string | null>(null);
  const [operations, setOperations] = useState<CloudOperations | null>(null);
  const [storageError, setStorageError] = useState("");
  const candidateIds = useRef(new Map<string, string>()); const bindingsController = useRef<AbortController | null>(null); const listGeneration = useRef(0); const listController = useRef<AbortController | null>(null);
  const identity = { origin: window.location.origin, authorityId, ownerScopeRef };
  const [browserOnline, setBrowserOnline] = useState(() => navigator.onLine);
  useEffect(() => { const sync = () => setBrowserOnline(navigator.onLine); window.addEventListener('online', sync); window.addEventListener('offline', sync); return () => { window.removeEventListener('online', sync); window.removeEventListener('offline', sync); }; }, []);
  const canWrite = browserOnline && lifecycle.enabled && lifecycle.health === "ready" && Array.isArray(lifecycle.features) && lifecycle.features.includes("durable-operations.v1") && !storageError;
  useEffect(() => {
    let value: CloudOperations | undefined; let alive = true;
    const initialize = async () => {
      try { value = new CloudOperations({ scope: { origin: window.location.origin, authorityId, ownerScopeRef, groupId: "" } }, product); value.setWritable(false); await value.refresh(); if (alive) { setOperations(value); setStorageError(""); } }
      catch { value?.dispose(); if (alive) setStorageError("浏览器持久操作存储不可用，当前仅供查看。"); }
    };
    void initialize(); return () => { alive = false; value?.dispose(); };
  }, [authorityId, ownerScopeRef, product]);
  useEffect(() => { if (operations) { operations.setWritable(canWrite); if (canWrite) void operations.recover(); else void operations.refresh(); } }, [operations, canWrite]);
  const reload = useCallback(async (cursor?: string) => {
    listController.current?.abort(); const controller = new AbortController(); listController.current = controller; const generation = ++listGeneration.current; setLoading(true); setError("");
    try { const result = await product.list({ origin: window.location.origin, authorityId, ownerScopeRef }, controller.signal, cursor); if (generation !== listGeneration.current || controller.signal.aborted) return; setGroups(previous => cursor ? [...previous.filter(row => !result.items.some(item => row.groupId === item.groupId)), ...result.items] : result.items); setNextCursor(result.nextCursor); }
    catch (cause) { if (!controller.signal.aborted) setError(messageOf(cause)); } finally { if (!controller.signal.aborted) setLoading(false); }
  }, [authorityId, ownerScopeRef, product]);
  useEffect(() => { void reload(); const sync = () => setSelected(selection().get("groupId") || ""); window.addEventListener("hashchange", sync); window.addEventListener("popstate", sync); return () => { listController.current?.abort(); window.removeEventListener("hashchange", sync); window.removeEventListener("popstate", sync); }; }, [reload]);
  const loadBindings = useCallback(async (cursor?: string) => {
    bindingsController.current?.abort(); const controller = new AbortController(); bindingsController.current = controller;
    setBindingsLoading(true); setBindingsError("");
    try {
      const result = await product.bindings({ origin: window.location.origin, authorityId, ownerScopeRef }, controller.signal, cursor);
      if (controller.signal.aborted) return;
      const next = result.items.map(binding => {
        let memberId = candidateIds.current.get(binding.bindingRef); if (!memberId) { memberId = `member-${crypto.randomUUID()}`; candidateIds.current.set(binding.bindingRef, memberId); }
        return { memberId, name: binding.name, binding: { ...binding, availability: {
          state: binding.availability.state, code: binding.availability.code ?? undefined,
          reason: binding.availability.reason ?? undefined, action: binding.availability.action ?? undefined,
        } } };
      });
      setCandidates(previous => cursor ? [...previous.filter(row => !next.some(item => item.binding.bindingRef === row.binding.bindingRef)), ...next] : next);
      setBindingsCursor(result.nextCursor || null);
    } catch (cause) { if (!controller.signal.aborted) setBindingsError(messageOf(cause)); }
    finally { if (!controller.signal.aborted) setBindingsLoading(false); }
  }, [authorityId, ownerScopeRef, product]);
  useEffect(() => { if (createOpen) void loadBindings(); return () => bindingsController.current?.abort(); }, [createOpen, loadBindings]);
  const choose = (id: string) => { setSelected(id); writeSelection(id); };
  const visible = groups.filter(group => `${group.name} ${group.lastMessage}`.toLowerCase().includes(query.toLowerCase()));
  return <div className="studio-teams-browser" data-selected={Boolean(selected)}>
    <aside className="studio-team-directory" aria-label="团队列表"><header><div><span className="studio-teams-eyebrow">云端协作空间</span><h2>团队</h2></div><button className="icon-button tertiary" aria-label="创建团队" disabled={!canWrite || !operations} onClick={() => setCreateOpen(true)}><Plus size={19} /></button></header>
      <label className="studio-team-search"><Search size={15} /><input aria-label="搜索已加载的团队" placeholder="搜索已加载的团队" value={query} onChange={event => setQuery(event.target.value)} /></label>
      {(!canWrite || storageError) && <div className="studio-team-list-error" role="status"><p>{storageError || "只读模式 · 服务准备或恢复中"}</p><button className="button tertiary" onClick={onRetry}>刷新服务状态</button></div>}
      {error && <div className="studio-team-list-error" role="alert"><p>{error}</p><button className="button tertiary" onClick={() => void reload()}>重试</button></div>}
      <div className="studio-team-list">{visible.map(group => <button key={group.groupId} className="studio-team-list-item" aria-current={selected === group.groupId ? "page" : undefined} onClick={() => choose(group.groupId)}><span className="studio-team-monogram" aria-hidden="true">{Array.from(group.name)[0]}</span><span className="studio-team-list-copy"><strong>{group.name}</strong><span>{group.lastMessage || `${group.memberCount} 位成员`}</span>{group.pendingCount > 0 && <small>{group.pendingCount} 项待处理</small>}</span></button>)}{loading && <p role="status">正在读取团队…</p>}{!loading && !visible.length && <div className="studio-team-list-empty"><p>{query ? "已加载团队中没有匹配项" : "还没有团队"}</p></div>}{nextCursor && <button className="button tertiary" disabled={loading} onClick={() => void reload(nextCursor)}>加载更多团队</button>}</div>
      {operations && <CreateOperationStatus operations={operations} canWrite={canWrite} onConfirmed={id => { choose(id); void reload(); }} />}
      <footer><a href="#/agents">管理 Agent</a><a href="#/plugins">插件设置</a></footer>
    </aside>
    <div className="studio-team-content">{selected ? <><button className="studio-team-directory-back" onClick={() => choose("")}><ArrowLeft size={16} />团队列表</button><CloudTeamWorkspace key={selected} scope={{ ...identity, groupId: selected }} transport={transport} product={product} canWrite={canWrite} effectsEnabled={lifecycle.features?.includes("effects.reconcile")} initialRunId={selection().get("teamRunId") || undefined} onSelectionChange={runId => writeSelection(selected, runId)} onChanged={() => void reload()} readOnlyReason={lifecycle.health === "degraded" ? "服务恢复中，已有历史仍可查看。" : undefined} /></> : <div className="studio-team-welcome"><span className="studio-teams-symbol"><UsersRound size={26} /></span><span className="studio-teams-eyebrow">AGENT TEAMS</span><h2>一个目标，团队一起完成</h2><p>选择团队，跟进协作任务、待处理事项和交付物。</p><button className="button primary" disabled={!canWrite || !operations} onClick={() => setCreateOpen(true)}><Plus size={16} />创建团队</button></div>}</div>
    <CreateGroupDialog open={createOpen} serverAuthority candidates={candidates} loading={bindingsLoading} error={bindingsError} onRefresh={() => void loadBindings()} hasMore={Boolean(bindingsCursor)} onLoadMore={() => { if (bindingsCursor) void loadBindings(bindingsCursor); }} onClose={() => setCreateOpen(false)} onCreate={async ({ idempotencyKey, ...payload }) => { if (!operations || !canWrite) throw new Error("当前无法创建团队，请恢复服务后重试。"); const result = await operations.submit({ operation: "groups", idempotencyKey, payload: payload as TeamsOperationPayload }); if (result.status !== "confirmed") throw new Error(result.status === "rejected" ? "服务端未接受创建请求，请核对成员配置。" : "创建结果尚未确认。已保留原单，请在团队列表下方核对。"); if (typeof result.receipt?.groupId === "string") choose(result.receipt.groupId); await reload(); }} />
  </div>;
}
function CreateOperationStatus({ operations, canWrite, onConfirmed }: { operations: CloudOperations; canWrite: boolean; onConfirmed: (id: string) => void }) {
  const state = useSyncExternalStore(operations.subscribe, operations.getSnapshot, operations.getSnapshot);
  const pending = state.operations.filter(operation => !["confirmed", "rejected"].includes(operation.status));
  const last = state.operations.filter(operation => operation.status === "confirmed").at(-1);
  return <div className="studio-team-list-error" aria-label="创建团队操作记录">{pending.length > 0 && <><p role="status">{pending.length} 项创建操作等待确认</p><button className="button tertiary" disabled={!canWrite || state.recovering} onClick={() => void operations.recover()}>{state.recovering ? "正在核对…" : "核对原单"}</button></>}{typeof last?.receipt?.groupId === "string" && <button className="button tertiary" onClick={() => onConfirmed(last.receipt!.groupId as string)}>打开最近创建的团队</button>}{state.errorCode && <p role="alert">操作存储暂不可用（{state.errorCode}）。</p>}</div>;
}

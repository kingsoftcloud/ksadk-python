import { useCallback, useEffect, useMemo, useState } from "react";
import { CloudUpload, RefreshCw, RotateCcw } from "lucide-react";
import { apiFetch } from "../api";
import { PageHeaderActions } from "../components/PageHeaderPortal";
import { showToast } from "../components/Toast";

interface Deployment {
  id: string;
  buildId: string;
  bundleDigest: string;
  versionId: string;
  status: "ADMITTING" | "DEPLOYING" | "READY" | "FAILED" | "ROLLED_BACK" | string;
  target: { region: string; environment: string };
  agentId?: string;
  instanceId?: string;
  artifactId?: string;
}

interface BuildCandidate {
  id: string;
  status: string;
  bundleDigest?: string;
}

const OPERATION_TERMINAL = new Set(["SUCCEEDED", "FAILED", "CANCELLED", "TIMED_OUT", "INTERRUPTED"]);

function deploymentState(status: string): "ready" | "failed" | "pending" | "idle" {
  if (status === "READY") return "ready";
  if (["FAILED", "ROLLED_BACK"].includes(status)) return "failed";
  if (["ADMITTING", "DEPLOYING"].includes(status)) return "pending";
  return "idle";
}

function deploymentLabel(status: string): string {
  return ({
    ADMITTING: "准入中",
    DEPLOYING: "部署中",
    READY: "已就绪",
    FAILED: "部署失败",
    ROLLED_BACK: "已回滚",
  } as Record<string, string>)[status] || "状态未知";
}

function shortId(value: string, max = 28): string {
  return value.length > max ? `${value.slice(0, max)}…` : value;
}

export function DeploymentsPage({ onCreate }: { onCreate: () => void }) {
  const [deployments, setDeployments] = useState<Deployment[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [refreshing, setRefreshing] = useState<Set<string>>(new Set());
  const [rollback, setRollback] = useState<{ deployment: Deployment; candidates: BuildCandidate[]; targetBuildId: string } | null>(null);
  const [rollbackBusy, setRollbackBusy] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const response = await apiFetch("/api/v1/deployments");
      if (!response.ok) throw new Error(`读取部署记录失败（${response.status}）`);
      const payload = await response.json();
      setDeployments(Array.isArray(payload.items) ? payload.items : []);
    } catch (caught: any) {
      setError(caught?.message || "部署记录不可用");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { void load(); }, [load]);

  const summary = useMemo(() => ({
    ready: deployments.filter(item => item.status === "READY").length,
    pending: deployments.filter(item => ["ADMITTING", "DEPLOYING"].includes(item.status)).length,
    failed: deployments.filter(item => ["FAILED", "ROLLED_BACK"].includes(item.status)).length,
  }), [deployments]);

  async function refresh(deployment: Deployment) {
    setRefreshing(current => new Set(current).add(deployment.id));
    try {
      const response = await apiFetch(`/api/v1/deployments/${encodeURIComponent(deployment.id)}`);
      if (!response.ok) throw new Error(`状态刷新失败（${response.status}）`);
      const updated = await response.json();
      setDeployments(current => current.map(item => item.id === deployment.id ? updated : item));
    } catch (caught: any) {
      setError(`${deployment.instanceId || deployment.id}：${caught?.message || "状态未知"}`);
    } finally {
      setRefreshing(current => {
        const next = new Set(current);
        next.delete(deployment.id);
        return next;
      });
    }
  }

  async function refreshAll() {
    await Promise.all(deployments.map(deployment => refresh(deployment)));
  }

  async function openRollback(deployment: Deployment) {
    setError("");
    try {
      const buildResponse = await apiFetch(`/api/v1/builds/${encodeURIComponent(deployment.buildId)}`);
      if (!buildResponse.ok) throw new Error(`读取部署 Build 失败（${buildResponse.status}）`);
      const build = await buildResponse.json();
      const sourceAgentId = String(build.agentId || "");
      if (!sourceAgentId) throw new Error("部署 receipt 缺少可回滚的源 Agent");
      const agentResponse = await apiFetch(`/api/v1/agents/${encodeURIComponent(sourceAgentId)}`);
      if (!agentResponse.ok) throw new Error(`读取源 Agent Build 历史失败（${agentResponse.status}）`);
      const agent = await agentResponse.json();
      const candidates = (agent.builds || []).filter((item: BuildCandidate) => (
        item.status === "SUCCEEDED" && item.id !== deployment.buildId
      ));
      if (!candidates.length) throw new Error("没有可用于回滚的历史成功 Build");
      setRollback({ deployment, candidates, targetBuildId: candidates[0].id });
    } catch (caught: any) {
      setError(caught?.message || "无法选择回滚 Build");
    }
  }

  async function submitRollback() {
    if (!rollback || rollbackBusy) return;
    setRollbackBusy(true);
    setError("");
    try {
      const response = await apiFetch(`/api/v1/deployments/${encodeURIComponent(rollback.deployment.id)}:rollback`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "Idempotency-Key": `rollback-${rollback.deployment.id}-${rollback.targetBuildId}`,
        },
        body: JSON.stringify({ targetBuildId: rollback.targetBuildId }),
      });
      if (!response.ok) throw new Error(`回滚提交失败（${response.status}）`);
      const operation = await response.json();
      let result: any;
      for (let attempt = 0; attempt < 150; attempt += 1) {
        await new Promise(resolve => setTimeout(resolve, 200));
        const status = await apiFetch(`/api/v1/operations/${encodeURIComponent(operation.id)}`);
        if (!status.ok) throw new Error(`回滚状态读取失败（${status.status}）`);
        result = await status.json();
        if (OPERATION_TERMINAL.has(result.status)) break;
      }
      if (!result || !OPERATION_TERMINAL.has(result.status)) throw new Error("回滚操作等待超时");
      if (result.status !== "SUCCEEDED") throw new Error(result.error?.message || "回滚未完成");
      setRollback(null);
      await load();
      showToast("已提交历史 Bundle 回滚", rollback.targetBuildId);
    } catch (caught: any) {
      setError(caught?.message || "回滚失败");
    } finally {
      setRollbackBusy(false);
    }
  }

  return (
    <div className="delivery-page" data-layout="document">
      <PageHeaderActions>
        <button className="button secondary" type="button" onClick={() => void refreshAll()} disabled={!deployments.length || refreshing.size > 0}>
          <RefreshCw size={15} /><span>刷新全部状态</span>
        </button>
      </PageHeaderActions>

      <div className="delivery-intro">
        <div><h2>预发部署</h2><p>显示 Studio receipt 与 Server 投影的实例状态；刷新才会读取云端状态。</p></div>
      </div>

      {error && <div className="form-error" role="alert">{error}</div>}

      <section className="delivery-stat-strip" aria-label="部署事实摘要">
        <div><span className="stat-label">部署记录</span><strong>{deployments.length}</strong><small>本地 receipt</small></div>
        <div><span className="stat-label">云端已就绪</span><strong>{summary.ready}</strong><small>Server 状态投影</small></div>
        <div><span className="stat-label">进行中</span><strong>{summary.pending}</strong><small>准入或实例启动</small></div>
        <div><span className="stat-label">失败或已回滚</span><strong>{summary.failed}</strong><small>需要查看操作结果</small></div>
        <div><span className="stat-label">目标</span><strong>预发</strong><small>preproduction</small></div>
      </section>

      {loading ? <div className="delivery-empty-state"><p>正在读取部署 receipt…</p></div> : !deployments.length ? (
        <div className="delivery-empty-state">
          <CloudUpload size={24} /><h2>还没有预发部署</h2>
          <p>先构建 AgentBundle，再从 Agent 详情发起受控准入和云端创建。</p>
          <button className="button accent" type="button" onClick={onCreate}>创建 Agent</button>
        </div>
      ) : (
        <section className="delivery-block" aria-label="部署生命周期">
          <h2>部署生命周期</h2><p>Bundle、准入与云端实例的每一行都有独立 receipt。</p>
          <div className="delivery-table-scroll">
            <table className="delivery-table">
              <thead><tr><th>状态</th><th>云端实例</th><th>Build</th><th>Bundle digest</th><th>目标</th><th><span className="sr-only">操作</span></th></tr></thead>
              <tbody>{deployments.map(deployment => {
                const refreshingThis = refreshing.has(deployment.id);
                return <tr key={deployment.id}>
                  <td><span className="delivery-status-badge" data-state={deploymentState(deployment.status)}>{deploymentLabel(deployment.status)}</span></td>
                  <td><code title={deployment.instanceId || deployment.id}>{deployment.instanceId || deployment.id}</code></td>
                  <td><code title={deployment.buildId}>{shortId(deployment.buildId)}</code></td>
                  <td><code title={deployment.bundleDigest}>{shortId(deployment.bundleDigest)}</code></td>
                  <td>{deployment.target.region}<small>{deployment.target.environment}</small></td>
                  <td className="delivery-row-actions">
                    <button className="button tertiary compact" type="button" aria-label="刷新部署状态" title="刷新部署状态" disabled={refreshingThis} onClick={() => void refresh(deployment)}><RefreshCw size={15} /></button>
                    <button className="button tertiary compact" type="button" aria-label="选择回滚 Build" onClick={() => void openRollback(deployment)}><RotateCcw size={15} /><span>回滚</span></button>
                  </td>
                </tr>;
              })}</tbody>
            </table>
          </div>
        </section>
      )}

      {rollback && (
        <section className="delivery-block rollback-panel" aria-label="选择回滚 Build">
          <div><h2>选择历史 Build</h2><p>将创建新的预发 deployment receipt，现有 receipt 不会被改写。</p></div>
          <label>
            <span>回滚目标</span>
            <select aria-label="选择回滚目标 Build" value={rollback.targetBuildId} onChange={event => setRollback({ ...rollback, targetBuildId: event.target.value })}>
              {rollback.candidates.map(candidate => <option key={candidate.id} value={candidate.id}>{candidate.id}{candidate.bundleDigest ? ` · ${shortId(candidate.bundleDigest)}` : ""}</option>)}
            </select>
          </label>
          <div className="rollback-actions">
            <button className="button tertiary" type="button" onClick={() => setRollback(null)} disabled={rollbackBusy}>取消</button>
            <button className="button accent" type="button" onClick={() => void submitRollback()} disabled={rollbackBusy}>{rollbackBusy ? "提交中" : "提交回滚"}</button>
          </div>
        </section>
      )}
    </div>
  );
}

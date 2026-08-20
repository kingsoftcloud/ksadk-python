import { useCallback, useEffect, useRef, useState } from "react";
import { Package, Plus } from "lucide-react";
import { apiFetch } from "../api";
import { PageHeaderActions } from "../components/PageHeaderPortal";
import { showToast } from "../components/Toast";

interface AgentSummary {
  metadata: { id: string; name: string; revision?: number };
}

interface BuildRecord {
  id: string;
  status: string;
  bundleDigest?: string;
  resolvedDigest?: string;
  runtimeName?: string;
  runtimeVersion?: string;
  sourceRevision?: number;
}

const TERMINAL = new Set(["SUCCEEDED", "FAILED", "CANCELLED", "TIMED_OUT"]);

function statusState(status: string): "ready" | "failed" | "pending" | "idle" {
  if (status === "SUCCEEDED") return "ready";
  if (["FAILED", "CANCELLED", "TIMED_OUT"].includes(status)) return "failed";
  if (status === "IDLE") return "idle";
  return "pending";
}

function statusLabel(status: string): string {
  return ({
    IDLE: "尚未构建",
    QUEUED: "排队中",
    RUNNING: "构建中",
    SUCCEEDED: "构建完成",
    FAILED: "构建失败",
    CANCELLED: "已取消",
    TIMED_OUT: "已超时",
  } as Record<string, string>)[status] || status;
}

function shortId(value: string, max = 42): string {
  return value.length > max ? `${value.slice(0, max)}…` : value;
}

async function fetchOperationEvents(path: string): Promise<any[]> {
  const response = await apiFetch(`/api/v1${path}`);
  if (!response.ok) return [];
  return (await response.text())
    .split("\n\n")
    .filter(Boolean)
    .map(block => {
      const data = block.split("\n").find(line => line.startsWith("data:"));
      if (!data) return null;
      try { return JSON.parse(data.slice(5).trim()); } catch { return null; }
    })
    .filter(Boolean);
}

export function BuildsPage({ currentAgentId, agents, onSelectAgent, onCreate }: {
  currentAgentId: string;
  agents: AgentSummary[];
  onSelectAgent: (id: string) => void;
  onCreate: () => void;
}) {
  void onSelectAgent;
  const [detail, setDetail] = useState<any>(null);
  const [status, setStatus] = useState("IDLE");
  const [log, setLog] = useState("选择 Agent 后开始本地构建。\n");
  const [operationId, setOperationId] = useState("");
  const [building, setBuilding] = useState(false);
  const logRef = useRef<HTMLPreElement>(null);
  const buildSeq = useRef(0);

  const loadDetail = useCallback(async () => {
    if (!currentAgentId) { setDetail(null); return; }
    try {
      const response = await apiFetch(`/api/v1/agents/${encodeURIComponent(currentAgentId)}`);
      if (!response.ok) throw new Error("Agent detail is unavailable");
      setDetail(await response.json());
    } catch {
      setDetail(null);
    }
  }, [currentAgentId]);

  useEffect(() => { void loadDetail(); }, [loadDetail]);

  const draft = detail?.draft;
  const builds: BuildRecord[] = detail?.builds || [];
  const latestBuild = builds.find(build => build.status === "SUCCEEDED") || builds[0];
  const selectedAgent = agents.find(agent => agent.metadata.id === currentAgentId);

  useEffect(() => {
    if (building) return;
    setStatus(latestBuild?.status || "IDLE");
    setOperationId("");
    setLog(latestBuild
      ? [
        `Build       ${latestBuild.id}`,
        `Revision    ${latestBuild.sourceRevision ?? draft?.metadata?.revision ?? "-"}`,
        `Bundle      ${latestBuild.bundleDigest || "-"}`,
        `Resolved    ${latestBuild.resolvedDigest || "-"}`,
        `Status      ${latestBuild.status}`,
      ].join("\n")
      : "选择 Agent 后开始本地构建。\n");
  }, [building, draft?.metadata?.revision, latestBuild]);

  function appendLog(entry: string) {
    setLog(previous => `${previous}${entry}`);
    requestAnimationFrame(() => {
      if (logRef.current) logRef.current.scrollTop = logRef.current.scrollHeight;
    });
  }

  async function build() {
    if (!draft || building) return;
    const sequence = ++buildSeq.current;
    setBuilding(true);
    setStatus("QUEUED");
    setOperationId("提交中");
    setLog("提交本地构建…\n");
    try {
      const response = await apiFetch(`/api/v1/agents/${encodeURIComponent(draft.metadata.id)}/builds`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "Idempotency-Key": `build-${draft.metadata.id}-r${draft.metadata.revision}-${Date.now()}`,
        },
        body: JSON.stringify({ revision: draft.metadata.revision, runEvaluation: false }),
      });
      if (!response.ok) throw new Error(`构建提交失败（${response.status}）`);
      const operation = await response.json();
      setOperationId(operation.id);
      let cursor = 0;
      let completed: any = null;
      for (let attempt = 0; attempt < 1200; attempt += 1) {
        if (buildSeq.current !== sequence) return;
        const events = await fetchOperationEvents(`/operations/${encodeURIComponent(operation.id)}/events?after=${cursor}`);
        if (events.length) {
          cursor = Math.max(cursor, ...events.map(event => Number(event.id) || 0));
          appendLog(events.map(event => `${String(event.id).padStart(2, "0")}  ${event.type}`).join("\n") + "\n");
        }
        const current = await apiFetch(`/api/v1/operations/${encodeURIComponent(operation.id)}`).then(item => item.json());
        setStatus(current.status || "QUEUED");
        if (TERMINAL.has(current.status)) { completed = current; break; }
        await new Promise(resolve => setTimeout(resolve, 200));
      }
      if (!completed) throw new Error("构建操作等待超时");
      if (completed.status !== "SUCCEEDED") throw new Error(completed.error?.message || "构建未完成");
      await loadDetail();
      showToast("不可变 Bundle 已构建", completed.resourceId || "构建完成");
    } catch (error: any) {
      if (buildSeq.current === sequence) {
        setStatus("FAILED");
        appendLog(`${error?.message || "构建失败"}\n`);
        showToast("构建失败", error?.message || "未知错误", "error");
      }
    } finally {
      if (buildSeq.current === sequence) setBuilding(false);
    }
  }

  const state = statusState(status);
  const runtime = latestBuild?.runtimeName
    ? `${latestBuild.runtimeName} ${latestBuild.runtimeVersion || ""}`.trim()
    : draft?.spec?.runtime?.type || "未选择";

  return (
    <div className="delivery-page" data-layout="document">
      <PageHeaderActions>
        <button className="button accent" type="button" onClick={build} disabled={!draft || building}>
          <Package size={15} /><span>{building ? "构建中" : "构建当前 Agent"}</span>
        </button>
      </PageHeaderActions>

      <div className="delivery-intro">
        <div><h2>不可变 Bundle</h2><p>解析当前 Revision，锁定依赖并生成可追溯交付物。</p></div>
        <span className="delivery-status-badge" data-state={state}>{statusLabel(status)}</span>
      </div>

      {agents.length === 0 ? (
        <div className="delivery-empty-state">
          <Package size={24} /><h2>还没有可构建的 Agent</h2>
          <p>先创建 Agent，再构建可部署到预发的 Bundle。</p>
          <button className="button accent" type="button" onClick={onCreate}><Plus size={15} /><span>创建 Agent</span></button>
        </div>
      ) : (
        <>
          <section className="delivery-stat-strip" aria-label="构建事实摘要">
            <div><span className="stat-label">当前 Agent</span><strong>{selectedAgent?.metadata.name || draft?.metadata?.name || "未选择"}</strong><small>{currentAgentId || "选择 Agent"}</small></div>
            <div><span className="stat-label">Revision</span><strong>{draft ? `r${draft.metadata.revision}` : "-"}</strong><small>构建输入</small></div>
            <div><span className="stat-label">构建状态</span><strong>{statusLabel(status)}</strong><small>{operationId || "最近记录"}</small></div>
            <div><span className="stat-label">Bundle digest</span><strong className="mono">{shortId(latestBuild?.bundleDigest || "-")}</strong><small>内容摘要</small></div>
            <div><span className="stat-label">Runtime</span><strong>{runtime}</strong><small>锁定 Profile</small></div>
          </section>

          <section className="delivery-block" aria-label="Bundle 事实链">
            <h2>构建事实链</h2><p>每一步都来自本地构建记录；云端状态不在这里推断。</p>
            <div className="delivery-fact-chain">
              <div className="delivery-fact-step" data-state={draft ? "ready" : "idle"}><span>输入 Revision</span><strong>{draft ? `r${draft.metadata.revision}` : "未选择"}</strong><code>{draft?.metadata?.id || "-"}</code></div>
              <div className="delivery-fact-step" data-state={state}><span>不可变 Bundle</span><strong>{latestBuild?.status === "SUCCEEDED" ? "已生成" : statusLabel(status)}</strong><code>{latestBuild?.bundleDigest || "尚无 digest"}</code></div>
              <div className="delivery-fact-step" data-state="idle"><span>预发部署</span><strong>尚未部署</strong><code>请从部署页提交准入</code></div>
            </div>
          </section>

          <details className="delivery-block" open={building}>
            <summary>构建技术日志 {operationId ? `· ${shortId(operationId, 28)}` : ""}</summary>
            <pre ref={logRef}>{log}</pre>
          </details>
        </>
      )}
    </div>
  );
}

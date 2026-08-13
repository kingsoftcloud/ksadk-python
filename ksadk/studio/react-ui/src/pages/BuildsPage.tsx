import { useCallback, useEffect, useRef, useState } from "react";
import { Package, Plus } from "lucide-react";
import { showToast } from "../components/Toast";
import { apiFetch } from "../api";

interface AgentSummary { metadata: { id: string; name: string; revision?: number } }

const TERMINAL = new Set(["SUCCEEDED", "FAILED", "CANCELLED", "TIMED_OUT"]);

async function fetchSse(path: string): Promise<any[]> {
  const response = await apiFetch(`/api/v1${path}`);
  if (!response.ok) return [];
  const text = await response.text();
  return text
    .split("\n\n")
    .filter(Boolean)
    .map(block => {
      const dataLine = block.split("\n").find(line => line.startsWith("data:"));
      if (!dataLine) return null;
      try { return JSON.parse(dataLine.slice(5).trim()); } catch { return null; }
    })
    .filter(Boolean);
}

function shortId(value: string, head = 36): string {
  if (!value) return "-";
  return value.length <= head ? value : `${value.slice(0, head)}…`;
}

export function BuildsPage({ currentAgentId, agents, onSelectAgent, onCreate }: {
  currentAgentId: string;
  agents: AgentSummary[];
  onSelectAgent: (id: string) => void;
  onCreate: () => void;
}) {
  void onSelectAgent; // 切换 Agent 统一走全局头部选择器。
  const [detail, setDetail] = useState<any>(null);
  const [status, setStatus] = useState("IDLE");
  const [log, setLog] = useState("选择 Agent 后开始本地构建。");
  const [operationId, setOperationId] = useState("等待构建");
  const [building, setBuilding] = useState(false);
  const logRef = useRef<HTMLPreElement>(null);
  const buildSeq = useRef(0);

  const loadDetail = useCallback(async () => {
    if (!currentAgentId) { setDetail(null); return; }
    try {
      const res = await apiFetch(`/api/v1/agents/${encodeURIComponent(currentAgentId)}`);
      if (!res.ok) throw new Error();
      setDetail(await res.json());
    } catch { setDetail(null); }
  }, [currentAgentId]);

  useEffect(() => { loadDetail(); }, [loadDetail]);

  const draft = detail?.draft;
  const builds: any[] = detail?.builds || [];
  const latestBuild = builds.find(b => b.status === "SUCCEEDED") || builds[0] || null;

  // 未构建时展示最近一次构建摘要。
  useEffect(() => {
    if (building) return;
    if (!draft) {
      setStatus("IDLE");
      setLog("选择 Agent 后开始本地构建。");
      setOperationId("等待构建");
      return;
    }
    setStatus(latestBuild?.status || "IDLE");
    if (latestBuild) {
      setLog([
        `Build       ${latestBuild.id || "-"}`,
        `Agent       ${latestBuild.agentId || "-"}`,
        `Revision    ${latestBuild.sourceRevision ?? "-"}`,
        `Resolved    ${latestBuild.resolvedDigest || "-"}`,
        `Bundle      ${latestBuild.bundleDigest || "-"}`,
        `Status      ${latestBuild.status || "-"}`,
      ].join("\n"));
    } else {
      setLog("选择 Agent 后开始本地构建。");
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [detail, building]);

  function appendLog(text: string) {
    setLog(prev => prev + text);
    requestAnimationFrame(() => {
      if (logRef.current) logRef.current.scrollTop = logRef.current.scrollHeight;
    });
  }

  async function build() {
    if (!draft || building) return;
    const seq = ++buildSeq.current;
    setBuilding(true);
    setLog("提交本地构建...\n");
    setStatus("QUEUED");
    setOperationId("提交中");
    try {
      const res = await apiFetch(`/api/v1/agents/${encodeURIComponent(draft.metadata.id)}/builds`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "Idempotency-Key": `build-${draft.metadata.id}-r${draft.metadata.revision}-${Date.now()}`,
        },
        body: JSON.stringify({ revision: draft.metadata.revision, runEvaluation: false }),
      });
      if (!res.ok) {
        const text = await res.text().catch(() => "");
        let msg = `构建提交失败（${res.status}）`;
        try { msg = JSON.parse(text)?.error?.message || msg; } catch {}
        throw new Error(msg);
      }
      const operation = await res.json();
      setOperationId(operation.id);

      let cursor = 0;
      let completed: any = null;
      for (let attempt = 0; attempt < 1200; attempt += 1) {
        if (buildSeq.current !== seq) return; // 切走 Agent 后停止写入
        const events = await fetchSse(`/operations/${encodeURIComponent(operation.id)}/events?after=${cursor}`);
        if (events.length) {
          cursor = Math.max(cursor, ...events.map(e => Number(e.id) || 0));
          appendLog(events.map(e => `${String(e.id).padStart(2, "0")}  ${e.type}`).join("\n") + "\n");
        }
        const op = await apiFetch(`/api/v1/operations/${encodeURIComponent(operation.id)}`).then(r => r.json());
        setStatus(op.status || "QUEUED");
        if (TERMINAL.has(op.status)) { completed = op; break; }
        await new Promise(r => setTimeout(r, 200));
      }
      if (!completed) throw new Error("操作等待超时");
      if (completed.status !== "SUCCEEDED") throw new Error(completed.error?.message || "构建未完成");
      const completedBuild = await apiFetch(`/api/v1/builds/${encodeURIComponent(completed.resourceId)}`).then(r => r.json());
      await loadDetail();
      showToast(
        `${draft.spec?.runtime?.type || "Agent"} Bundle 构建完成`,
        shortId(completedBuild.bundleDigest || ""),
      );
    } catch (e: any) {
      if (buildSeq.current === seq) {
        setStatus("FAILED");
        appendLog(`\n${e.message}\n`);
        showToast("构建失败", e.message, "error");
      }
    } finally {
      if (buildSeq.current === seq) setBuilding(false);
    }
  }

  const runtimeText = latestBuild?.runtimeName
    ? `${latestBuild.runtimeName} ${latestBuild.runtimeVersion || ""}`.trim()
    : draft?.spec?.runtime?.type || "未选择";

  return (
    <div className="page-container" data-layout="data" data-scroll-mode="data">
      <header className="page-header">
        <div><h1>构建</h1><p>将 Agent Draft 解析为不可变、可追溯的 AgentBundle。</p></div>
        <button className="button accent" type="button" onClick={build} disabled={!draft || building}>
          <Package size={15} /><span>{building ? "构建中" : "构建当前 Agent"}</span>
        </button>
      </header>
      <div className="data-page-body">
        {agents.length === 0 && (
          <div className="empty-state">
            <span className="empty-icon"><Package size={24} /></span>
            <h2>还没有可构建的 Agent</h2>
            <p>先创建一个 Agent，再来构建与运行。</p>
            <button className="button accent" type="button" onClick={onCreate}><Plus size={15} /><span>创建 Agent</span></button>
          </div>
        )}
        <div className="build-workspace">
          <div className="build-summary">
            <div><span>当前 Agent</span><strong>{draft?.metadata?.name || "未选择"}</strong></div>
            <div><span>Revision</span><strong>{draft ? `r${draft.metadata.revision}` : "-"}</strong></div>
            <div><span>状态</span><strong><span className={`status-badge ${status}`}>{status}</span></strong></div>
            <div><span>Bundle</span><strong className="mono truncate">{latestBuild?.bundleDigest || "-"}</strong></div>
            <div><span>Source SHA</span><strong className="mono truncate">{latestBuild?.manifestSha256 || latestBuild?.sourceDigest || latestBuild?.resolvedDigest || "-"}</strong></div>
            <div><span>Runtime</span><strong>{runtimeText}</strong></div>
          </div>
          <div className="build-log">
            <div className="log-header"><span>构建事件</span><span>{operationId}</span></div>
            <pre ref={logRef}>{log}</pre>
          </div>
        </div>
      </div>
    </div>
  );
}

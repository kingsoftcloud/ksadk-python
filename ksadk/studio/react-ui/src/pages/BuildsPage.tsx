import { useCallback, useEffect, useRef, useState } from "react";
import { CloudUpload, Package, Plus } from "lucide-react";
import { apiFetch } from "../api";
import { PageHeaderActions } from "../components/PageHeaderPortal";
import { showToast } from "../components/Toast";
import { deploymentCreateRoute, navigateToStudioHash } from "../studioRoutes";

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
  const deployable = latestBuild?.status === "SUCCEEDED" && !building;
  const selectedAgent = agents.find(agent => agent.metadata.id === currentAgentId);
  const artifactType = draft?.metadata?.labels?.["agentkit.ksyun.com/artifact-type"];
  const isManagedRuntime = artifactType === "ManagedRuntime"
    || (!artifactType && draft?.spec?.runtime?.type === "codex");
  const deliveryLabel = (value: string) => isManagedRuntime
    ? ({
      IDLE: "尚未校验",
      QUEUED: "校验排队中",
      RUNNING: "正在校验",
      SUCCEEDED: "声明已校验",
      FAILED: "校验失败",
      CANCELLED: "已取消",
      TIMED_OUT: "校验超时",
    } as Record<string, string>)[value] || value
    : statusLabel(value);

  useEffect(() => {
    if (building) return;
    setStatus(latestBuild?.status || "IDLE");
    setOperationId("");
    setLog(latestBuild
      ? [
        `${isManagedRuntime ? "Declaration" : "Build"}       ${latestBuild.id}`,
        `Revision    ${latestBuild.sourceRevision ?? draft?.metadata?.revision ?? "-"}`,
        `${isManagedRuntime ? "YAML digest" : "Bundle"}   ${latestBuild.bundleDigest || "-"}`,
        `Resolved    ${latestBuild.resolvedDigest || "-"}`,
        `Status      ${latestBuild.status}`,
      ].join("\n")
      : isManagedRuntime ? "选择 YAML Agent 后校验声明。\n" : "选择 Agent 后开始本地构建。\n");
  }, [building, draft?.metadata?.revision, isManagedRuntime, latestBuild]);

  function appendLog(entry: string) {
    setLog(previous => `${previous}${entry}`);
    requestAnimationFrame(() => {
      if (logRef.current) logRef.current.scrollTop = logRef.current.scrollHeight;
    });
  }

  function openDeploymentFlow() {
    if (!currentAgentId || !deployable) return;
    onSelectAgent(currentAgentId);
    navigateToStudioHash(deploymentCreateRoute(latestBuild.id, currentAgentId));
  }

  async function build() {
    if (!draft || building) return;
    const sequence = ++buildSeq.current;
    setBuilding(true);
    setStatus("QUEUED");
    setOperationId("提交中");
    setLog(isManagedRuntime ? "提交 YAML 声明校验…\n" : "提交本地构建…\n");
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
      showToast(isManagedRuntime ? "YAML 声明已校验" : "不可变 Bundle 已构建", completed.resourceId || "构建完成");
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
          <Package size={15} /><span>{building ? (isManagedRuntime ? "校验中" : "构建中") : (isManagedRuntime ? "校验 YAML 声明" : "构建当前 Agent")}</span>
        </button>
      </PageHeaderActions>

      <div className="delivery-intro">
        <div>
          <h2>{isManagedRuntime ? "ManagedRuntime 声明" : "Code Bundle"}</h2>
          <p>{isManagedRuntime
            ? "校验 YAML 声明并锁定 Runtime、模型与能力摘要，生成可追溯的托管运行时制品。"
            : "打包 ADK、LangGraph 等代码 Agent，锁定代码、依赖与能力摘要。"}</p>
        </div>
        <span className="delivery-status-badge" data-state={state}>{deliveryLabel(status)}</span>
      </div>

      {agents.length === 0 ? (
        <div className="delivery-empty-state">
          <Package size={24} /><h2>还没有可构建的 Agent</h2>
          <p>先创建 Agent，再生成可部署到云端的交付记录。</p>
          <button className="button accent" type="button" onClick={onCreate}><Plus size={15} /><span>创建 Agent</span></button>
        </div>
      ) : (
        <>
          <section className="delivery-stat-strip" aria-label="构建事实摘要">
            <div><span className="stat-label">当前 Agent</span><strong>{selectedAgent?.metadata.name || draft?.metadata?.name || "未选择"}</strong><small>{currentAgentId || "选择 Agent"}</small></div>
            <div><span className="stat-label">Revision</span><strong>{draft ? `r${draft.metadata.revision}` : "-"}</strong><small>构建输入</small></div>
            <div><span className="stat-label">{isManagedRuntime ? "校验状态" : "构建状态"}</span><strong>{deliveryLabel(status)}</strong><small>{operationId || "最近记录"}</small></div>
            <div><span className="stat-label">{isManagedRuntime ? "YAML 摘要" : "Bundle digest"}</span><strong className="mono">{shortId(latestBuild?.bundleDigest || "-")}</strong><small>{isManagedRuntime ? "声明内容摘要" : "内容摘要"}</small></div>
            <div><span className="stat-label">Runtime</span><strong>{runtime}</strong><small>锁定 Profile</small></div>
          </section>

          <section className="delivery-block" aria-label={isManagedRuntime ? "ManagedRuntime 事实链" : "Code Bundle 事实链"}>
            <h2>{isManagedRuntime ? "声明事实链" : "构建事实链"}</h2><p>每一步都来自本地交付记录；部署页负责选择目标并提交云端操作。</p>
            <div className="delivery-fact-chain">
              <div className="delivery-fact-step" data-state={draft ? "ready" : "idle"}><span>{isManagedRuntime ? "输入 YAML Revision" : "输入 Revision"}</span><strong>{draft ? `r${draft.metadata.revision}` : "未选择"}</strong><code>{draft?.metadata?.id || "-"}</code></div>
              <div className="delivery-fact-step" data-state={state}><span>{isManagedRuntime ? "声明摘要" : "不可变 Bundle"}</span><strong>{latestBuild?.status === "SUCCEEDED" ? (isManagedRuntime ? "已校验" : "已生成") : deliveryLabel(status)}</strong><code>{latestBuild?.bundleDigest || "尚无 digest"}</code></div>
              <div className="delivery-fact-step" data-state={deployable ? "ready" : "idle"}>
                <span>下一步</span>
                <strong>{deployable ? "构建完成，下一步可部署到云端" : "等待构建完成"}</strong>
                <code>{deployable ? `使用 ${latestBuild.id} 进入统一部署流程` : "成功 Build 生成后开放部署入口"}</code>
                {deployable && (
                  <div className="delivery-empty-actions">
                    <button className="button secondary compact" type="button" onClick={openDeploymentFlow}>
                      <CloudUpload size={15} />部署到云端
                    </button>
                  </div>
                )}
              </div>
            </div>
          </section>

          <details className="delivery-block" open={building}>
            <summary>{isManagedRuntime ? "声明校验日志" : "构建技术日志"} {operationId ? `· ${shortId(operationId, 28)}` : ""}</summary>
            <pre ref={logRef}>{log}</pre>
          </details>
        </>
      )}
    </div>
  );
}

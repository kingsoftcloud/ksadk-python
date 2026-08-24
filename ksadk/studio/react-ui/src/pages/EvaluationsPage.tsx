import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Activity, FileUp, Play, RefreshCw } from "lucide-react";
import { apiFetch } from "../api";
import { Drawer } from "../components/Drawer";
import { showToast } from "../components/Toast";
import { FormField } from "../components/ui/FormField";
import { StudioDataTable, type StudioDataColumn } from "../components/ui/StudioDataTable";
import { StudioSelect } from "../components/ui/StudioSelect";
import {
  ACTIVE_EVALUATION_STATES,
  evaluationElapsed,
  evaluationErrorMessage,
  evaluationStatusClass,
  formatEvaluationDate,
  type EvaluationRun,
} from "./evaluationTypes";
import "./evaluations.css";

type TargetKind = "a2a" | "local_source" | "studio_build";
type EvaluatorId =
  | "response_contract@v1"
  | "runtime_budget@v1"
  | "tool_trajectory@v1"
  | "reference_match@v1";

interface EvaluationCatalog {
  builds: Array<{ id: string; agentId: string; runtime: string }>;
}

interface StudioAgent {
  metadata: { id: string; name: string };
}

const EVALUATOR_OPTIONS: Array<{ id: EvaluatorId; label: string }> = [
  { id: "response_contract@v1", label: "响应契约" },
  { id: "runtime_budget@v1", label: "运行预算" },
  { id: "tool_trajectory@v1", label: "工具轨迹" },
  { id: "reference_match@v1", label: "参考答案匹配" },
];
const DEFAULT_EVALUATORS: EvaluatorId[] = EVALUATOR_OPTIONS.slice(0, 3).map(option => option.id);

export function EvaluationsPage({
  refreshTick,
  onOpenRun = runId => window.history.pushState(null, "", `#/evaluations/${encodeURIComponent(runId)}`),
}: {
  refreshTick: number;
  onOpenRun?: (runId: string) => void;
}) {
  const requestController = useRef<AbortController | null>(null);
  const requestSeq = useRef(0);
  const [runs, setRuns] = useState<EvaluationRun[]>([]);
  const [catalog, setCatalog] = useState<EvaluationCatalog>({ builds: [] });
  const [agents, setAgents] = useState<StudioAgent[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState("");
  const [formOpen, setFormOpen] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [completionMessage, setCompletionMessage] = useState("");
  const [evalsetFile, setEvalsetFile] = useState("");
  const [evalsetUploading, setEvalsetUploading] = useState(false);
  const [evalsetError, setEvalsetError] = useState("");
  const [targetKind, setTargetKind] = useState<TargetKind>("a2a");
  const [selectedAgentId, setSelectedAgentId] = useState("");
  const [targetLocator, setTargetLocator] = useState("");
  const [timeoutSeconds, setTimeoutSeconds] = useState(120);
  const [failFast, setFailFast] = useState(false);
  const [selectedEvaluators, setSelectedEvaluators] = useState<EvaluatorId[]>(() => [...DEFAULT_EVALUATORS]);

  const loadRuns = useCallback(async () => {
    const seq = ++requestSeq.current;
    requestController.current?.abort();
    const controller = new AbortController();
    requestController.current = controller;
    try {
      const response = await apiFetch("/api/v1/evaluation-runs", { signal: controller.signal });
      if (!response.ok) throw new Error(await evaluationErrorMessage(response, "评测任务加载失败"));
      const payload = await response.json();
      if (seq === requestSeq.current) {
        setRuns(payload.items || []);
        setLoadError("");
      }
    } catch (error) {
      if (controller.signal.aborted) return;
      if (seq === requestSeq.current) {
        setLoadError(error instanceof Error ? error.message : "评测任务加载失败");
      }
    } finally {
      if (seq === requestSeq.current) setLoading(false);
    }
  }, []);

  const loadCatalog = useCallback(async () => {
    try {
      const [targetResponse, agentResponse] = await Promise.all([
        apiFetch("/api/v1/evaluation-targets"),
        apiFetch("/api/v1/agents"),
      ]);
      if (!targetResponse.ok) return;
      const [payload, agentPayload] = await Promise.all([
        targetResponse.json(),
        agentResponse.ok ? agentResponse.json() : Promise.resolve({ items: [] }),
      ]);
      setCatalog({ builds: payload.builds || [] });
      setAgents(agentPayload.items || []);
    } catch {
      // Target 目录不可用不影响任务列表。
    }
  }, []);

  useEffect(() => {
    setLoading(true);
    void loadRuns();
    void loadCatalog();
  }, [loadCatalog, loadRuns, refreshTick]);

  useEffect(() => {
    if (!runs.some(run => ACTIVE_EVALUATION_STATES.has(run.status))) return;
    const refresh = () => { if (document.visibilityState === "visible") void loadRuns(); };
    const timer = window.setInterval(refresh, 1000);
    document.addEventListener("visibilitychange", refresh);
    return () => {
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", refresh);
    };
  }, [loadRuns, runs]);

  useEffect(() => () => requestController.current?.abort(), []);

  function changeTargetKind(kind: TargetKind) {
    setTargetKind(kind);
    if (kind !== "studio_build") {
      setTargetLocator("");
      return;
    }
    const agentId = agents.find(agent => (
      catalog.builds.some(build => build.agentId === agent.metadata.id)
    ))?.metadata.id || catalog.builds[0]?.agentId || "";
    setSelectedAgentId(agentId);
    setTargetLocator(catalog.builds.find(build => build.agentId === agentId)?.id || "");
  }

  const selectedAgentBuilds = useMemo(
    () => catalog.builds.filter(build => build.agentId === selectedAgentId),
    [catalog.builds, selectedAgentId],
  );

  useEffect(() => {
    if (targetKind !== "studio_build") return;
    setSelectedAgentId(current => (
      current && catalog.builds.some(build => build.agentId === current)
        ? current
        : agents.find(agent => catalog.builds.some(build => build.agentId === agent.metadata.id))?.metadata.id
          || catalog.builds[0]?.agentId
          || ""
    ));
  }, [agents, catalog.builds, targetKind]);

  useEffect(() => {
    if (targetKind !== "studio_build") return;
    setTargetLocator(current => (
      selectedAgentBuilds.some(build => build.id === current)
        ? current
        : selectedAgentBuilds[0]?.id || ""
    ));
  }, [selectedAgentBuilds, targetKind]);

  async function submitEvaluation(event: React.FormEvent) {
    event.preventDefault();
    setSubmitting(true);
    setCompletionMessage("");
    try {
      const response = await apiFetch("/api/v1/evaluations", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "Idempotency-Key": `evaluation-${Date.now()}`,
        },
        body: JSON.stringify({
          evalsetFile: evalsetFile.trim(),
          target: { kind: targetKind, locator: targetLocator.trim() },
          config: {
            timeoutSeconds,
            failFast,
            dataPolicy: "local_only",
            evaluators: selectedEvaluators,
          },
        }),
      });
      if (!response.ok) throw new Error(await evaluationErrorMessage(response, "评测任务创建失败"));
      setFormOpen(false);
      setCompletionMessage("评测任务已创建");
      showToast("评测任务已创建", evalsetFile.trim());
      await loadRuns();
    } catch (error) {
      showToast("评测任务创建失败", error instanceof Error ? error.message : "请稍后重试", "error");
    } finally {
      setSubmitting(false);
    }
  }

  async function importEvalset(file: File) {
    setEvalsetUploading(true);
    setEvalsetError("");
    const body = new FormData();
    body.append("file", file);
    try {
      const response = await apiFetch("/api/v1/evaluation-files", { method: "POST", body });
      if (!response.ok) throw new Error(await evaluationErrorMessage(response, "EvalSet 文件导入失败"));
      const payload = await response.json();
      setEvalsetFile(payload.path || "");
    } catch (error) {
      setEvalsetError(error instanceof Error ? error.message : "EvalSet 文件导入失败");
    } finally {
      setEvalsetUploading(false);
    }
  }

  const columns = useMemo<StudioDataColumn<EvaluationRun>[]>(() => [
    {
      id: "evalset",
      header: "EvalSet / Run",
      minWidth: 260,
      cell: run => <><strong>{run.evalset.name || "未命名 EvalSet"}</strong><span className="resource-origin mono">{run.id}</span></>,
    },
    {
      id: "target",
      header: "Target",
      minWidth: 180,
      cell: run => <><span>{run.target.label || run.target.kind || "-"}</span><span className="resource-origin mono">{run.target.kind || "-"}</span></>,
    },
    {
      id: "status",
      header: "状态",
      width: 120,
      cell: run => <span className={`status-badge ${evaluationStatusClass(run.status)}`}>{run.status}</span>,
    },
    {
      id: "progress",
      header: "进度 / Case",
      minWidth: 150,
      cell: run => run.summary
        ? <><strong>{run.summary.passedCases} / {run.summary.totalCases}</strong><span className="resource-origin">通过</span></>
        : run.progress
          ? <><strong>{run.progress.current} / {run.progress.total}</strong><span className="resource-origin">{run.progress.caseId || "执行中"}</span></>
          : <span>等待开始</span>,
    },
    { id: "createdAt", header: "创建时间", minWidth: 150, cell: run => formatEvaluationDate(run.createdAt) },
    { id: "duration", header: "耗时", width: 100, cell: evaluationElapsed },
  ], []);

  const activeCount = runs.filter(run => ACTIVE_EVALUATION_STATES.has(run.status)).length;
  const completedRuns = runs.filter(run => run.hasReport);
  const passedCount = completedRuns.filter(run => run.status === "PASSED").length;
  const abnormalCount = runs.filter(run => ["FAILED", "ERROR", "INTERRUPTED"].includes(run.status)).length;
  const targetLocatorLabel = targetKind === "a2a"
    ? "Agent 地址"
    : targetKind === "local_source"
      ? "Agent 源码目录"
      : "Build";

  return (
    <div className="page-container evaluation-page" data-layout="data" data-scroll-mode="data">
      <header className="page-header">
        <div><h1>评测</h1><p>创建、监控并检查 Agent 的评测运行。</p></div>
        <div className="header-actions">
          <button className="button tertiary" type="button" onClick={() => void loadRuns()} aria-label="刷新评测任务">
            <RefreshCw size={15} /><span>刷新</span>
          </button>
          <button className="button accent" type="button" onClick={() => setFormOpen(true)}>
            <Play size={15} /><span>新建评测</span>
          </button>
        </div>
      </header>

      {completionMessage && <p className="sr-only" role="status">{completionMessage}</p>}

      <section className="evaluation-page__metrics" aria-label="评测汇总">
        <div><span>评测运行</span><strong>{runs.length}</strong><small>全部任务</small></div>
        <div><span>运行中</span><strong>{activeCount}</strong><small>排队或执行</small></div>
        <div><span>已通过</span><strong>{passedCount}</strong><small>{completedRuns.length} 个已有报告</small></div>
        <div><span>异常</span><strong>{abnormalCount}</strong><small>失败、错误或中断</small></div>
      </section>

      <section className="evaluation-page__run-list" aria-label="评测运行">
        <div className="evaluation-page__panel-header"><div><strong>评测运行</strong><span>{runs.length} 个任务</span></div></div>
        <StudioDataTable
          columns={columns}
          data={runs}
          getRowId={run => run.id}
          caption="评测运行列表"
          minWidth={980}
          loading={loading}
          error={loadError}
          onRetry={() => void loadRuns()}
          onRowActivate={run => onOpenRun(run.id)}
          rowAriaLabel={run => `打开评测 ${run.evalset.name || run.id}`}
          empty={{ icon: <Activity size={22} />, title: "还没有评测任务", description: "新建评测后，任务会立即显示在这里。" }}
        />
      </section>

      {formOpen && (
        <Drawer
          title="新建评测"
          subtitle="选择 EvalSet、Target 和评估器。任务创建后将在后台执行。"
          wide
          closeDisabled={submitting}
          onClose={() => setFormOpen(false)}
          footer={(
            <>
              <span className="drawer-footer-spacer" />
              <button className="button tertiary" type="button" onClick={() => setFormOpen(false)} disabled={submitting}>取消</button>
              <button className="button accent" type="submit" form="evaluation-create-form" disabled={submitting || evalsetUploading || !evalsetFile.trim() || !targetLocator.trim() || !selectedEvaluators.length}>
                {submitting ? "正在创建" : "开始评测"}
              </button>
            </>
          )}
        >
          <form id="evaluation-create-form" className="evaluation-page__create form-grid two-columns" onSubmit={submitEvaluation}>
            <FormField className="evaluation-page__field--wide" label="EvalSet 文件" htmlFor="evaluation-evalset" requirement="required">
              <div className="evaluation-page__evalset-picker">
                <input
                  id="evaluation-evalset"
                  className="sr-only"
                  type="file"
                  accept=".yaml,.yml,.json,application/json,application/yaml,text/yaml"
                  aria-label="选择 EvalSet 文件"
                  disabled={evalsetUploading}
                  onChange={event => {
                    const file = event.target.files?.[0];
                    event.target.value = "";
                    if (file) void importEvalset(file);
                  }}
                />
                <label className="button secondary" htmlFor="evaluation-evalset">
                  <FileUp size={15} /><span>{evalsetUploading ? "正在导入" : "选择文件"}</span>
                </label>
                <span className="evaluation-page__evalset-path" title={evalsetFile}>{evalsetFile || "尚未选择 EvalSet"}</span>
              </div>
              {evalsetError && <p className="studio-field-error" role="alert">{evalsetError}</p>}
            </FormField>

            <FormField label="Target 类型" requirement="required">
              <StudioSelect
                ariaLabel="Target 类型"
                value={targetKind}
                options={[
                  { value: "a2a", label: "A2A Agent" },
                  { value: "local_source", label: "本地源码" },
                  { value: "studio_build", label: "Studio Build" },
                ]}
                onValueChange={value => changeTargetKind(value as TargetKind)}
              />
            </FormField>
            {targetKind === "studio_build" && (
              <FormField label="Agent" htmlFor="evaluation-agent" requirement="required">
                <StudioSelect
                  id="evaluation-agent"
                  ariaLabel="Studio Agent"
                  value={selectedAgentId}
                  placeholder="请选择 Agent"
                  options={agents.map(agent => ({
                    value: agent.metadata.id,
                    label: agent.metadata.name,
                    description: catalog.builds.some(build => build.agentId === agent.metadata.id)
                      ? agent.metadata.id
                      : `${agent.metadata.id} · 需先构建`,
                    disabled: !catalog.builds.some(build => build.agentId === agent.metadata.id),
                  }))}
                  disabled={!agents.some(agent => catalog.builds.some(build => build.agentId === agent.metadata.id))}
                  onValueChange={setSelectedAgentId}
                />
              </FormField>
            )}
            <FormField label={targetLocatorLabel} htmlFor="evaluation-locator" requirement="required">
              {targetKind === "studio_build" ? (
                <StudioSelect
                  id="evaluation-locator"
                  ariaLabel="Studio Build"
                  value={targetLocator}
                  placeholder="暂无成功 Build"
                  options={selectedAgentBuilds.map(build => ({ value: build.id, label: build.id, description: build.runtime }))}
                  disabled={!selectedAgentBuilds.length}
                  onValueChange={setTargetLocator}
                />
              ) : (
                <input id="evaluation-locator" value={targetLocator} onChange={event => setTargetLocator(event.target.value)} required placeholder={targetKind === "a2a" ? "https://agent.example.test/a2a" : "."} />
              )}
            </FormField>
            <FormField label="超时（秒）" htmlFor="evaluation-timeout" requirement="required">
              <input id="evaluation-timeout" type="number" min={1} max={3600} value={timeoutSeconds} onChange={event => setTimeoutSeconds(Number(event.target.value))} required />
            </FormField>
            <FormField label="运行策略">
              <label className="checkbox-row evaluation-page__fail-fast">
                <input type="checkbox" checked={failFast} onChange={event => setFailFast(event.target.checked)} />
                <span><strong>Fail fast</strong><small>首个失败 Case 后停止</small></span>
              </label>
            </FormField>

            <FormField className="evaluation-page__field--wide" label="评估器" requirement="required">
              <div className="evaluation-page__evaluator-options" role="group" aria-label="评估器">
                {EVALUATOR_OPTIONS.map(option => (
                  <label className="checkbox-row" key={option.id}>
                    <input
                      type="checkbox"
                      aria-label={option.label}
                      checked={selectedEvaluators.includes(option.id)}
                      onChange={event => setSelectedEvaluators(current => (
                        event.target.checked ? [...current, option.id] : current.filter(id => id !== option.id)
                      ))}
                    />
                    <span><strong>{option.label}</strong><small>{option.id}</small></span>
                  </label>
                ))}
              </div>
              {!selectedEvaluators.length && <p className="studio-field-error" role="alert">至少选择一个评估器</p>}
            </FormField>
          </form>
        </Drawer>
      )}
    </div>
  );
}

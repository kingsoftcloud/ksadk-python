import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Activity, ChevronDown, ChevronUp, Play, RefreshCw } from "lucide-react";
import { apiFetch } from "../api";
import { showToast } from "../components/Toast";
import { FormField } from "../components/ui/FormField";
import { StudioDataTable, type StudioDataColumn } from "../components/ui/StudioDataTable";
import { StudioSelect } from "../components/ui/StudioSelect";
import "./evaluations.css";

type TargetKind = "a2a" | "local_source" | "studio_build";

interface MetricResult {
  name: string;
  status: string;
  score?: number | null;
  required?: boolean;
  evidence?: Record<string, unknown>;
}

interface CaseRun {
  caseId: string;
  attempt: number;
  targetRun: {
    status: string;
    output?: string;
    durationMs?: number | null;
    errorMessage?: string | null;
    traceRef?: Record<string, unknown> | null;
  };
  metrics: MetricResult[];
}

interface EvaluationReport {
  spec: {
    id: string;
    evalset: { name: string; contentDigest?: string };
    target: {
      kind: string;
      entrypoint: string;
      revisionDigest: string;
      runtime?: string;
    };
  };
  status: string;
  createdAt: string;
  summary: {
    totalCases: number;
    passedCases: number;
    failedCases: number;
    unavailableCases: number;
    errorCases: number;
    cancelledCases: number;
  };
  caseRuns: CaseRun[];
}

interface Operation {
  id: string;
  status: string;
  resourceId?: string | null;
  error?: { message?: string } | null;
}

interface OperationEvent {
  id: number;
  type: string;
  data: { caseId?: string; index?: number; total?: number };
}

interface EvaluationCatalog {
  evalsets: Array<{ path: string; name: string; caseCount: number }>;
  builds: Array<{ id: string; agentId: string; runtime: string }>;
}

const TERMINAL_OPERATION_STATES = new Set(["SUCCEEDED", "FAILED", "CANCELLED", "INTERRUPTED"]);
const OPERATION_POLL_TIMEOUT_MS = 3_600_000;

function formatDate(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "-";
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}

function formatDuration(value?: number | null): string {
  if (value === null || value === undefined) return "-";
  return value < 1000 ? `${value} ms` : `${(value / 1000).toFixed(2)} s`;
}

function reportStatusClass(status: string): string {
  if (status === "PASSED") return "success";
  if (status === "PENDING" || status === "RUNNING") return "info";
  return "danger";
}

function caseStatus(caseRun: CaseRun): string {
  if (["ERROR", "CANCELLED", "UNAVAILABLE"].includes(caseRun.targetRun.status)) {
    return caseRun.targetRun.status;
  }
  return caseRun.metrics.every(metric => !metric.required || metric.status === "PASS")
    ? "PASSED"
    : "FAILED";
}

async function errorMessage(response: Response, fallback: string): Promise<string> {
  try {
    const payload = await response.json();
    return payload?.error?.message || payload?.detail?.message || fallback;
  } catch {
    return fallback;
  }
}

async function waitForOperation(
  operationId: string,
  signal: AbortSignal,
  onEvent: (event: OperationEvent) => void,
): Promise<Operation> {
  const deadline = Date.now() + OPERATION_POLL_TIMEOUT_MS;
  let eventCursor = 0;
  for (;;) {
    signal.throwIfAborted();
    if (Date.now() >= deadline) throw new Error("评测任务等待超时，请稍后刷新报告查看最终状态");
    const response = await apiFetch(`/api/v1/operations/${encodeURIComponent(operationId)}`, { signal });
    if (!response.ok) throw new Error(await errorMessage(response, "评测任务状态读取失败"));
    const operation: Operation = await response.json();
    if (TERMINAL_OPERATION_STATES.has(operation.status)) return operation;
    const eventsResponse = await apiFetch(
      `/api/v1/operations/${encodeURIComponent(operationId)}/events?after=${eventCursor}`,
      { signal, headers: { Accept: "application/json" } },
    );
    if (eventsResponse.ok) {
      const payload = await eventsResponse.json();
      for (const event of payload.items || []) {
        eventCursor = Math.max(eventCursor, event.id || 0);
        onEvent(event);
      }
    }
    await new Promise<void>((resolve, reject) => {
      const abort = () => {
        window.clearTimeout(timer);
        reject(new DOMException("Operation polling aborted", "AbortError"));
      };
      const timer = window.setTimeout(() => {
        signal.removeEventListener("abort", abort);
        resolve();
      }, 350);
      signal.addEventListener("abort", abort, { once: true });
    });
  }
}

export function EvaluationsPage({ refreshTick }: { refreshTick: number }) {
  const operationController = useRef<AbortController | null>(null);
  const [reports, setReports] = useState<EvaluationReport[]>([]);
  const [catalog, setCatalog] = useState<EvaluationCatalog>({ evalsets: [], builds: [] });
  const [activeReport, setActiveReport] = useState<EvaluationReport | null>(null);
  const [activeCaseId, setActiveCaseId] = useState("");
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState("");
  const [formOpen, setFormOpen] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [completionMessage, setCompletionMessage] = useState("");
  const [activeOperation, setActiveOperation] = useState<Operation | null>(null);
  const [currentCase, setCurrentCase] = useState<{ caseId: string; index: number; total: number } | null>(null);
  const [evalsetFile, setEvalsetFile] = useState("");
  const [targetKind, setTargetKind] = useState<TargetKind>("a2a");
  const [targetLocator, setTargetLocator] = useState("");
  const [timeoutSeconds, setTimeoutSeconds] = useState(120);
  const [failFast, setFailFast] = useState(false);

  const loadReports = useCallback(async () => {
    setLoading(true);
    setLoadError("");
    try {
      const response = await apiFetch("/api/v1/evaluations");
      if (!response.ok) throw new Error(await errorMessage(response, "评测报告加载失败"));
      const payload = await response.json();
      const items: EvaluationReport[] = payload.items || [];
      setReports(items);
      setActiveReport(current => items.find(item => item.spec.id === current?.spec.id) || null);
    } catch (error) {
      setLoadError(error instanceof Error ? error.message : "评测报告加载失败");
    } finally {
      setLoading(false);
    }
  }, []);

  const loadCatalog = useCallback(async () => {
    try {
      const response = await apiFetch("/api/v1/evaluation-targets");
      if (!response.ok) return;
      const payload = await response.json();
      const next: EvaluationCatalog = {
        evalsets: payload.evalsets || [],
        builds: payload.builds || [],
      };
      setCatalog(next);
      setEvalsetFile(current => current || next.evalsets[0]?.path || "");
    } catch {
      // 目录不可用时保留手动输入，不影响评测报告列表。
    }
  }, []);

  useEffect(() => {
    void loadReports();
    void loadCatalog();
  }, [loadCatalog, loadReports, refreshTick]);

  useEffect(() => () => {
    const controller = operationController.current;
    operationController.current = null;
    controller?.abort();
  }, []);

  function changeTargetKind(kind: TargetKind) {
    setTargetKind(kind);
    if (kind === "studio_build") {
      setTargetLocator(current => (
        catalog.builds.some(build => build.id === current)
          ? current
          : catalog.builds[0]?.id || ""
      ));
    }
  }

  const openReportById = useCallback(async (reportId: string, allowMissing = false) => {
    try {
      const response = await apiFetch(`/api/v1/evaluations/${encodeURIComponent(reportId)}`);
      if (allowMissing && response.status === 404) return;
      if (!response.ok) throw new Error(await errorMessage(response, "评测详情加载失败"));
      const detail: EvaluationReport = await response.json();
      setActiveReport(detail);
      setActiveCaseId(detail.caseRuns[0]?.caseId || "");
    } catch (error) {
      showToast("评测详情加载失败", error instanceof Error ? error.message : "请稍后重试", "error");
    }
  }, []);

  const openReport = useCallback(async (report: EvaluationReport) => {
    await openReportById(report.spec.id);
  }, [openReportById]);

  const columns = useMemo<StudioDataColumn<EvaluationReport>[]>(() => [
    {
      id: "evalset",
      header: "EvalSet / Run",
      minWidth: 260,
      cell: report => <><strong>{report.spec.evalset.name}</strong><span className="resource-origin mono">{report.spec.id}</span></>,
    },
    {
      id: "target",
      header: "Target Snapshot",
      minWidth: 220,
      cell: report => <><span>{report.spec.target.runtime || report.spec.target.kind}</span><span className="resource-origin mono">{report.spec.target.revisionDigest}</span></>,
    },
    {
      id: "status",
      header: "状态",
      width: 110,
      cell: report => <span className={`status-badge ${reportStatusClass(report.status)}`}>{report.status}</span>,
    },
    {
      id: "cases",
      header: "通过 Case",
      width: 120,
      cell: report => `${report.summary.passedCases} / ${report.summary.totalCases}`,
    },
    { id: "createdAt", header: "创建时间", minWidth: 150, cell: report => formatDate(report.createdAt) },
  ], []);

  const totals = reports.reduce((result, report) => ({
    cases: result.cases + report.summary.totalCases,
    passed: result.passed + report.summary.passedCases,
    failed: result.failed + report.summary.failedCases + report.summary.errorCases + report.summary.unavailableCases,
  }), { cases: 0, passed: 0, failed: 0 });
  const activeCase = activeReport?.caseRuns.find(item => item.caseId === activeCaseId) || null;

  async function submitEvaluation(event: React.FormEvent) {
    event.preventDefault();
    setSubmitting(true);
    setCompletionMessage("");
    operationController.current?.abort();
    const controller = new AbortController();
    operationController.current = controller;
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
            evaluators: [],
          },
        }),
      });
      if (!response.ok) throw new Error(await errorMessage(response, "评测任务创建失败"));
      const queued: Operation = await response.json();
      setActiveOperation(queued);
      const completed = await waitForOperation(queued.id, controller.signal, event => {
        if (event.type !== "evaluation.case.started") return;
        const { caseId, index, total } = event.data;
        if (caseId && index && total) setCurrentCase({ caseId, index, total });
      });
      if (completed.status === "CANCELLED") {
        setCompletionMessage("评测任务已取消");
        showToast("评测已取消", "已完成的 Case 将保留在部分报告中");
        await loadReports();
        if (completed.resourceId) await openReportById(completed.resourceId, true);
        setFormOpen(false);
        return;
      }
      if (completed.status !== "SUCCEEDED") {
        throw new Error(completed.error?.message || `评测任务状态：${completed.status}`);
      }
      if (!completed.resourceId) throw new Error("评测任务已完成，但未返回报告 ID");
      setCompletionMessage("评测任务已完成");
      showToast("评测已完成", evalsetFile.trim());
      await loadReports();
      await openReportById(completed.resourceId);
      setFormOpen(false);
    } catch (error) {
      if (error instanceof DOMException && error.name === "AbortError") return;
      showToast("评测执行失败", error instanceof Error ? error.message : "请稍后重试", "error");
    } finally {
      if (operationController.current === controller) {
        operationController.current = null;
        setSubmitting(false);
        setActiveOperation(null);
        setCurrentCase(null);
      }
    }
  }

  async function cancelEvaluation() {
    if (!activeOperation || TERMINAL_OPERATION_STATES.has(activeOperation.status)) return;
    const response = await apiFetch(`/api/v1/operations/${encodeURIComponent(activeOperation.id)}:cancel`, {
      method: "POST",
    });
    if (!response.ok) {
      showToast("取消评测失败", await errorMessage(response, "请稍后重试"), "error");
    }
  }

  return (
    <div className="page-container evaluation-page" data-layout="data" data-scroll-mode="data">
      <header className="page-header">
        <div><h1>评测</h1><p>按固定 EvalSet 运行 Agent Target，查看 Case 结果、指标与证据。</p></div>
        <div className="header-actions">
          <button className="button tertiary" type="button" onClick={() => void loadReports()} aria-label="刷新评测报告">
            <RefreshCw size={15} /><span>刷新</span>
          </button>
          <button className="button accent" type="button" onClick={() => setFormOpen(value => !value)} aria-expanded={formOpen}>
            <Play size={15} /><span>新建评测</span>{formOpen ? <ChevronUp size={14} /> : <ChevronDown size={14} />}
          </button>
        </div>
      </header>

      {activeOperation && (
        <div className="evaluation-page__operation" role="status">
          <span>{currentCase ? `Case ${currentCase.index} / ${currentCase.total}：${currentCase.caseId}` : "评测任务准备中"}</span>
          <button className="button tertiary" type="button" onClick={() => void cancelEvaluation()}>取消评测</button>
        </div>
      )}

      {formOpen && (
        <form className="evaluation-page__create" onSubmit={submitEvaluation}>
          <FormField label="EvalSet 文件" htmlFor="evaluation-evalset" requirement="required">
            {catalog.evalsets.length ? (
              <StudioSelect
                id="evaluation-evalset"
                ariaLabel="EvalSet 文件"
                value={evalsetFile}
                options={catalog.evalsets.map(item => ({
                  value: item.path,
                  label: `${item.name} · ${item.caseCount} Cases`,
                  description: item.path,
                }))}
                onValueChange={setEvalsetFile}
              />
            ) : (
              <input id="evaluation-evalset" value={evalsetFile} onChange={event => setEvalsetFile(event.target.value)} required placeholder="evalsets/smoke.yaml" />
            )}
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
          <FormField label="Target locator" htmlFor="evaluation-locator" requirement="required">
            {targetKind === "studio_build" && catalog.builds.length ? (
              <StudioSelect
                id="evaluation-locator"
                ariaLabel="Studio Build"
                value={targetLocator}
                options={catalog.builds.map(build => ({
                  value: build.id,
                  label: `${build.agentId} · ${build.runtime}`,
                  description: build.id,
                }))}
                onValueChange={setTargetLocator}
              />
            ) : (
              <input id="evaluation-locator" value={targetLocator} onChange={event => setTargetLocator(event.target.value)} required placeholder={targetKind === "a2a" ? "https://agent.example.test/a2a" : "."} />
            )}
          </FormField>
          <FormField label="超时（秒）" htmlFor="evaluation-timeout" requirement="required">
            <input id="evaluation-timeout" type="number" min={1} max={3600} value={timeoutSeconds} onChange={event => setTimeoutSeconds(Number(event.target.value))} required />
          </FormField>
          <label className="checkbox-row evaluation-page__fail-fast">
            <input type="checkbox" checked={failFast} onChange={event => setFailFast(event.target.checked)} />
            <span><strong>Fail fast</strong><small>首个失败 Case 后停止</small></span>
          </label>
          <div className="evaluation-page__form-actions">
            <button className="button tertiary" type="button" onClick={() => setFormOpen(false)}>取消</button>
            <button className="button accent" type="submit" disabled={submitting}>{submitting ? "执行中" : "开始评测"}</button>
          </div>
        </form>
      )}

      {completionMessage && <p className="sr-only" role="status">{completionMessage}</p>}

      <section className="evaluation-page__metrics" aria-label="评测汇总">
        <div><span>评测运行</span><strong>{reports.length}</strong><small>已持久化报告</small></div>
        <div><span>Case 总数</span><strong>{totals.cases}</strong><small>全部运行</small></div>
        <div><span>通过率</span><strong>{totals.cases ? `${Math.round(totals.passed / totals.cases * 100)}%` : "-"}</strong><small>{totals.passed} 个通过</small></div>
        <div><span>未通过</span><strong>{totals.failed}</strong><small>失败、错误或不可用</small></div>
      </section>

      <section className="evaluation-page__workbench">
        <div className="evaluation-page__runs">
          <div className="evaluation-page__panel-header"><div><strong>评测运行</strong><span>{reports.length} 条报告</span></div></div>
          <StudioDataTable
            columns={columns}
            data={reports}
            getRowId={report => report.spec.id}
            caption="评测运行列表"
            minWidth={760}
            loading={loading}
            error={loadError}
            onRetry={() => void loadReports()}
            onRowActivate={report => void openReport(report)}
            rowAriaLabel={report => `打开评测 ${report.spec.evalset.name}`}
            empty={{ icon: <Activity size={22} />, title: "还没有评测报告", description: "新建评测后，运行报告会显示在这里。" }}
          />
        </div>

        <div className="evaluation-page__detail">
          {!activeReport ? (
            <div className="evaluation-page__detail-empty"><Activity size={22} /><strong>选择一条评测运行</strong><span>查看 Target Snapshot、Case、指标和 TraceRef。</span></div>
          ) : (
            <>
              <div className="evaluation-page__panel-header"><div><strong>{activeReport.spec.evalset.name}</strong><span className="mono">{activeReport.spec.id}</span></div><span className={`status-badge ${reportStatusClass(activeReport.status)}`}>{activeReport.status}</span></div>
              <dl className="evaluation-page__snapshot">
                <div><dt>Target</dt><dd>{activeReport.spec.target.kind}</dd></div>
                <div><dt>Runtime</dt><dd>{activeReport.spec.target.runtime || "-"}</dd></div>
                <div><dt>Revision Digest</dt><dd className="mono">{activeReport.spec.target.revisionDigest}</dd></div>
                <div><dt>Entrypoint</dt><dd className="mono">{activeReport.spec.target.entrypoint}</dd></div>
              </dl>
              <div className="evaluation-page__case-layout">
                <div className="evaluation-page__case-list" aria-label="Case 列表">
                  {activeReport.caseRuns.map(caseRun => (
                    <button key={caseRun.caseId} type="button" className={caseRun.caseId === activeCaseId ? "active" : ""} onClick={() => setActiveCaseId(caseRun.caseId)}>
                      <span><strong>{caseRun.caseId}</strong><small>Attempt {caseRun.attempt}</small></span>
                      <span><span className={`status-badge ${reportStatusClass(caseStatus(caseRun))}`}>{caseStatus(caseRun)}</span><small>{formatDuration(caseRun.targetRun.durationMs)}</small></span>
                    </button>
                  ))}
                </div>
                <div className="evaluation-page__case-detail">
                  {activeCase ? (
                    <>
                      <section><h3>Agent 输出</h3><pre>{activeCase.targetRun.output || activeCase.targetRun.errorMessage || "无输出"}</pre></section>
                      <section><h3>指标</h3><div className="evaluation-page__evidence">{activeCase.metrics.map(metric => <div key={`${metric.name}-${metric.status}`}><strong>{metric.name}</strong><span className={`status-badge ${metric.status === "PASS" ? "success" : "danger"}`}>{metric.status}</span><span>{metric.score ?? "-"}</span></div>)}</div></section>
                      <section><h3>TraceRef</h3><pre>{activeCase.targetRun.traceRef ? JSON.stringify(activeCase.targetRun.traceRef, null, 2) : "未上报 TraceRef"}</pre></section>
                    </>
                  ) : <div className="evaluation-page__detail-empty"><span>选择一个 Case 查看详情。</span></div>}
                </div>
              </div>
            </>
          )}
        </div>
      </section>
    </div>
  );
}

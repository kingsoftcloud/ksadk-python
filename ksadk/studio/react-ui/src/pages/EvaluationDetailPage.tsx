import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Activity, ArrowLeft, RefreshCw, Square } from "lucide-react";
import { apiFetch } from "../api";
import { showToast } from "../components/Toast";
import {
  ACTIVE_EVALUATION_STATES,
  evaluationCaseStatus,
  evaluationElapsed,
  evaluationErrorMessage,
  evaluationStatusClass,
  formatEvaluationDate,
  formatEvaluationDuration,
  type EvaluationAssertion,
  type EvaluationRun,
} from "./evaluationTypes";
import "./evaluations.css";

function formatSnapshotValue(value: unknown): string {
  if (typeof value === "string") return value;
  if (value === undefined || value === null) return "-";
  return JSON.stringify(value, null, 2);
}

function assertionResult(
  assertion: EvaluationAssertion,
  index: number,
  assertions: EvaluationAssertion[],
  metrics: NonNullable<EvaluationRun["report"]>["caseRuns"][number]["metrics"],
) {
  const occurrence = assertions
    .slice(0, index)
    .filter(item => item.type === assertion.type).length;
  return metrics
    .filter(metric => metric.evidence?.assertion === assertion.type)[occurrence];
}

export function EvaluationDetailPage({ runId, onBack }: { runId: string; onBack: () => void }) {
  const requestController = useRef<AbortController | null>(null);
  const [run, setRun] = useState<EvaluationRun | null>(null);
  const [activeCaseId, setActiveCaseId] = useState("");
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState("");
  const [cancelling, setCancelling] = useState(false);

  const loadRun = useCallback(async () => {
    requestController.current?.abort();
    const controller = new AbortController();
    requestController.current = controller;
    try {
      const response = await apiFetch(`/api/v1/evaluation-runs/${encodeURIComponent(runId)}`, {
        signal: controller.signal,
      });
      if (!response.ok) throw new Error(await evaluationErrorMessage(response, "评测详情加载失败"));
      const next: EvaluationRun = await response.json();
      setRun(next);
      setActiveCaseId(current => (
        next.report?.caseRuns.some(item => item.caseId === current)
          ? current
          : next.report?.caseRuns[0]?.caseId || ""
      ));
      setLoadError("");
    } catch (error) {
      if (controller.signal.aborted) return;
      setLoadError(error instanceof Error ? error.message : "评测详情加载失败");
    } finally {
      if (!controller.signal.aborted) setLoading(false);
    }
  }, [runId]);

  useEffect(() => {
    setLoading(true);
    void loadRun();
    return () => requestController.current?.abort();
  }, [loadRun]);

  useEffect(() => {
    if (!run || !ACTIVE_EVALUATION_STATES.has(run.status)) return;
    const refresh = () => { if (document.visibilityState === "visible") void loadRun(); };
    const timer = window.setInterval(refresh, 1000);
    document.addEventListener("visibilitychange", refresh);
    return () => {
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", refresh);
    };
  }, [loadRun, run]);

  const activeCase = useMemo(
    () => run?.report?.caseRuns.find(item => item.caseId === activeCaseId) || null,
    [activeCaseId, run],
  );
  const caseSpecs = useMemo(
    () => new Map((run?.report?.spec.evalset.cases || []).map(item => [item.id, item])),
    [run],
  );
  const activeCaseSpec = activeCase ? caseSpecs.get(activeCase.caseId) : undefined;

  async function cancelEvaluation() {
    if (!run || !ACTIVE_EVALUATION_STATES.has(run.status) || cancelling) return;
    setCancelling(true);
    try {
      const response = await apiFetch(`/api/v1/operations/${encodeURIComponent(run.operationId)}:cancel`, {
        method: "POST",
      });
      if (!response.ok) throw new Error(await evaluationErrorMessage(response, "取消评测失败"));
      showToast("已提交取消请求", run.evalset.name || run.id);
      await loadRun();
    } catch (error) {
      showToast("取消评测失败", error instanceof Error ? error.message : "请稍后重试", "error");
    } finally {
      setCancelling(false);
    }
  }

  return (
    <div className="page-container evaluation-page evaluation-detail-page" data-layout="data" data-scroll-mode="workbench">
      <header className="page-header">
        <div>
          <h1>{run?.evalset.name || "评测详情"}</h1>
          <p className="mono">{runId}</p>
        </div>
        <div className="header-actions">
          <button className="button tertiary" type="button" onClick={onBack}>
            <ArrowLeft size={15} /><span>返回评测列表</span>
          </button>
          <button className="button secondary" type="button" onClick={() => void loadRun()}>
            <RefreshCw size={15} /><span>刷新</span>
          </button>
          {run && ACTIVE_EVALUATION_STATES.has(run.status) && (
            <button className="button danger" type="button" onClick={() => void cancelEvaluation()} disabled={cancelling}>
              <Square size={14} /><span>{cancelling ? "正在取消" : "取消评测"}</span>
            </button>
          )}
        </div>
      </header>

      {loading && !run ? (
        <div className="evaluation-page__detail-empty"><Activity size={22} /><strong>正在加载评测详情</strong></div>
      ) : loadError && !run ? (
        <div className="evaluation-page__detail-empty" role="alert">
          <strong>评测详情加载失败</strong><span>{loadError}</span>
          <button className="button secondary" type="button" onClick={() => void loadRun()}>重试</button>
        </div>
      ) : run ? (
        <>
          <section className="evaluation-detail-page__overview" aria-label="评测运行概览">
            <div><span>状态</span><strong><span className={`status-badge ${evaluationStatusClass(run.status)}`}>{run.status}</span></strong><small>{run.error?.message || "任务状态"}</small></div>
            <div><span>进度</span><strong>{run.progress ? `${run.progress.current} / ${run.progress.total}` : run.summary ? `${run.summary.passedCases} / ${run.summary.totalCases}` : "等待开始"}</strong><small>{run.progress?.caseId || (run.hasReport ? "通过 Case" : "尚未执行 Case")}</small></div>
            <div><span>Target</span><strong>{run.target.label || run.target.kind || "-"}</strong><small>{run.target.kind || "-"}</small></div>
            <div><span>耗时</span><strong>{evaluationElapsed(run)}</strong><small>{formatEvaluationDate(run.createdAt)}</small></div>
          </section>

          {!run.report && (
            <section className="evaluation-detail-page__pending">
              <Activity size={20} />
              <div>
                <strong>{ACTIVE_EVALUATION_STATES.has(run.status) ? "评测正在后台执行" : "该任务没有可用报告"}</strong>
                <span>{run.progress?.caseId ? `当前 Case：${run.progress.caseId}` : run.error?.message || "等待运行状态更新。"}</span>
              </div>
            </section>
          )}

          {run.report && (
            <section className="evaluation-detail-page__report">
              <div className="evaluation-page__panel-header">
                <div><strong>评测报告</strong><span>{run.report.caseRuns.length} 个 Case</span></div>
                <span className={`status-badge ${evaluationStatusClass(run.report.status)}`}>{run.report.status}</span>
              </div>
              <dl className="evaluation-page__snapshot">
                <div><dt>Target</dt><dd>{run.report.spec.target.kind}</dd></div>
                <div><dt>Runtime</dt><dd>{run.report.spec.target.runtime || "-"}</dd></div>
                <div><dt>Revision Digest</dt><dd className="mono">{run.report.spec.target.revisionDigest}</dd></div>
                <div><dt>Entrypoint</dt><dd className="mono">{run.report.spec.target.entrypoint}</dd></div>
              </dl>
              <section className="evaluation-page__dataset" aria-labelledby="evaluation-dataset-heading">
                <div className="evaluation-page__dataset-heading">
                  <div>
                    <h2 id="evaluation-dataset-heading">数据集快照</h2>
                    {typeof run.report.spec.evalset.metadata?.description === "string" && (
                      <p>{run.report.spec.evalset.metadata.description}</p>
                    )}
                  </div>
                  <span>{run.report.spec.evalset.schemaVersion || "ksadk.eval/v1"}</span>
                </div>
                <dl className="evaluation-page__dataset-summary">
                  <div><dt>名称</dt><dd>{run.report.spec.evalset.name}</dd></div>
                  <div><dt>Case</dt><dd>{run.report.spec.evalset.cases?.length ?? run.report.caseRuns.length}</dd></div>
                  <div><dt>来源格式</dt><dd>{run.report.spec.evalset.sourceFormat || "-"}</dd></div>
                  <div><dt>Content Digest</dt><dd className="mono">{run.report.spec.evalset.contentDigest || "-"}</dd></div>
                </dl>
              </section>
              <div className="evaluation-page__case-layout">
                <div className="evaluation-page__case-list" aria-label="Case 列表">
                  {run.report.caseRuns.map(caseRun => {
                    const status = evaluationCaseStatus(caseRun);
                    const caseSpec = caseSpecs.get(caseRun.caseId);
                    const inputPreview = caseSpec?.turns.at(-1)?.input;
                    return (
                      <button key={caseRun.caseId} type="button" className={caseRun.caseId === activeCaseId ? "active" : ""} onClick={() => setActiveCaseId(caseRun.caseId)}>
                        <span><strong>{caseRun.caseId}</strong><small className="evaluation-page__case-preview">{inputPreview || `Attempt ${caseRun.attempt}`}</small></span>
                        <span><span className={`status-badge ${evaluationStatusClass(status)}`}>{status}</span><small>{formatEvaluationDuration(caseRun.targetRun.durationMs)}</small></span>
                      </button>
                    );
                  })}
                </div>
                <div className="evaluation-page__case-detail">
                  {activeCase ? (
                    <>
                      <section>
                        <h3>输入与预期</h3>
                        {activeCaseSpec ? (
                          <div className="evaluation-page__turns">
                            {activeCaseSpec.turns.map((turn, index) => (
                              <article key={`${activeCaseSpec.id}-turn-${index}`}>
                                <strong>Turn {index + 1}</strong>
                                <div><span>输入</span><pre>{turn.input}</pre></div>
                                {turn.expectedOutput !== undefined && turn.expectedOutput !== null && (
                                  <div><span>期望输出</span><pre>{turn.expectedOutput}</pre></div>
                                )}
                                {!!turn.expectedTools?.length && (
                                  <div>
                                    <span>期望工具</span>
                                    <div className="evaluation-page__expected-tools">
                                      {turn.expectedTools.map((tool, toolIndex) => (
                                        <code key={`${index}-${toolIndex}`}>{String(tool.name || formatSnapshotValue(tool))}</code>
                                      ))}
                                    </div>
                                  </div>
                                )}
                              </article>
                            ))}
                          </div>
                        ) : <p className="evaluation-page__muted">该报告没有保存 Case 输入快照。</p>}
                      </section>
                      {!!activeCaseSpec?.assertions?.length && (
                        <section>
                          <h3>断言与评估结果</h3>
                          <div className="evaluation-page__assertions">
                            {activeCaseSpec.assertions.map((assertion, index, assertions) => {
                              const metric = assertionResult(assertion, index, assertions, activeCase.metrics);
                              return (
                                <div key={`${assertion.type}-${index}`}>
                                  <span><strong>{assertion.type}</strong><small>{assertion.required === false ? "可选" : "必需"}</small></span>
                                  <code>{formatSnapshotValue(assertion.value)}</code>
                                  <span className={`status-badge ${evaluationStatusClass(metric?.status === "PASS" ? "PASSED" : metric?.status || "UNAVAILABLE")}`}>{metric?.status || "-"}</span>
                                </div>
                              );
                            })}
                          </div>
                        </section>
                      )}
                      <section>
                        <div className="evaluation-page__section-title">
                          <h3>Agent 输出</h3>
                          <span>{formatEvaluationDuration(activeCase.targetRun.durationMs)} · {activeCase.targetRun.usage?.reported ? `${activeCase.targetRun.usage.totalTokens || 0} Tokens` : "Token 未上报"}</span>
                        </div>
                        <pre>{activeCase.targetRun.output || activeCase.targetRun.errorMessage || "无输出"}</pre>
                      </section>
                      <section><h3>评估指标</h3><div className="evaluation-page__evidence">{activeCase.metrics.map(metric => <div key={`${metric.name}-${metric.version || "v1"}-${metric.status}`}><strong>{metric.name}@{metric.version || "v1"}</strong><span className={`status-badge ${evaluationStatusClass(metric.status === "PASS" ? "PASSED" : metric.status)}`}>{metric.status}</span><span>{metric.score ?? "-"}</span></div>)}</div></section>
                      <section className="evaluation-page__case-evidence">
                        <details>
                          <summary>执行证据</summary>
                          <h4>TraceRef</h4>
                          <pre>{activeCase.targetRun.traceRef ? JSON.stringify(activeCase.targetRun.traceRef, null, 2) : "未上报 TraceRef"}</pre>
                          {!!activeCaseSpec && Object.keys(activeCaseSpec.metadata || {}).length > 0 && (
                            <><h4>Case Metadata</h4><pre>{JSON.stringify(activeCaseSpec.metadata, null, 2)}</pre></>
                          )}
                        </details>
                      </section>
                    </>
                  ) : <div className="evaluation-page__detail-empty"><span>选择一个 Case 查看详情。</span></div>}
                </div>
              </div>
            </section>
          )}
        </>
      ) : null}
    </div>
  );
}

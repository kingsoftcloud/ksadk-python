export interface MetricResult {
  name: string;
  version?: string;
  status: string;
  score?: number | null;
  required?: boolean;
  evidence?: Record<string, unknown>;
}

export interface EvaluationTurn {
  input: string;
  expectedOutput?: string | null;
  expectedTools?: Array<Record<string, unknown>>;
  metadata?: Record<string, unknown>;
}

export interface EvaluationAssertion {
  type: string;
  value: unknown;
  required?: boolean;
}

export interface EvaluationCase {
  id: string;
  turns: EvaluationTurn[];
  assertions?: EvaluationAssertion[];
  metadata?: Record<string, unknown>;
}

export interface CaseRun {
  caseId: string;
  attempt: number;
  targetRun: {
    status: string;
    output?: string;
    durationMs?: number | null;
    errorMessage?: string | null;
    traceRef?: Record<string, unknown> | null;
    usage?: {
      inputTokens?: number;
      outputTokens?: number;
      totalTokens?: number;
      reported?: boolean;
    };
    toolCalls?: Array<Record<string, unknown>>;
    metadata?: Record<string, unknown>;
  };
  metrics: MetricResult[];
}

export interface EvaluationSummary {
  totalCases: number;
  passedCases: number;
  failedCases: number;
  unavailableCases: number;
  errorCases: number;
  cancelledCases: number;
}

export interface EvaluationReport {
  spec: {
    id: string;
    evalset: {
      schemaVersion?: string;
      name: string;
      cases?: EvaluationCase[];
      metadata?: Record<string, unknown>;
      sourceFormat?: string;
      contentDigest?: string;
    };
    target: {
      kind: string;
      entrypoint: string;
      revisionDigest: string;
      runtime?: string;
    };
  };
  status: string;
  createdAt: string;
  summary: EvaluationSummary;
  caseRuns: CaseRun[];
}

export interface EvaluationRun {
  id: string;
  operationId: string;
  status: string;
  createdAt: string;
  completedAt?: string | null;
  evalset: { name?: string; caseCount?: number };
  target: { kind?: string; label?: string };
  evaluators: string[];
  progress?: { current: number; total: number; caseId?: string | null } | null;
  summary?: EvaluationSummary | null;
  hasReport: boolean;
  error?: { code?: string; message?: string } | null;
  report?: EvaluationReport | null;
}

export const ACTIVE_EVALUATION_STATES = new Set(["QUEUED", "RUNNING"]);

export function formatEvaluationDate(value?: string | null): string {
  if (!value) return "-";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "-";
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}

export function formatEvaluationDuration(value?: number | null): string {
  if (value === null || value === undefined) return "-";
  return value < 1000 ? `${value} ms` : `${(value / 1000).toFixed(2)} s`;
}

export function evaluationElapsed(run: EvaluationRun): string {
  if (!run.completedAt) return ACTIVE_EVALUATION_STATES.has(run.status) ? "进行中" : "-";
  const duration = new Date(run.completedAt).getTime() - new Date(run.createdAt).getTime();
  return Number.isFinite(duration) && duration >= 0 ? formatEvaluationDuration(duration) : "-";
}

export function evaluationStatusClass(status: string): string {
  if (status === "PASSED" || status === "SUCCEEDED") return "success";
  if (ACTIVE_EVALUATION_STATES.has(status)) return "info";
  if (status === "CANCELLED" || status === "INTERRUPTED") return "warning";
  return "danger";
}

export function evaluationCaseStatus(caseRun: CaseRun): string {
  if (["ERROR", "CANCELLED", "UNAVAILABLE"].includes(caseRun.targetRun.status)) {
    return caseRun.targetRun.status;
  }
  return caseRun.metrics.every(metric => !metric.required || metric.status === "PASS")
    ? "PASSED"
    : "FAILED";
}

export async function evaluationErrorMessage(response: Response, fallback: string): Promise<string> {
  try {
    const payload = await response.json();
    return payload?.error?.message || payload?.detail?.message || fallback;
  } catch {
    return fallback;
  }
}

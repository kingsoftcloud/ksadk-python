import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ArrowLeft, CloudUpload, ExternalLink, MessagesSquare, Package, RefreshCw } from "lucide-react";
import { apiFetch } from "../api";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { MoreActionsMenu } from "../components/MoreActionsMenu";
import { PageHeaderActions } from "../components/PageHeaderPortal";
import { showToast } from "../components/Toast";
import {
  mergeCloudChatTargets,
  resolveCloudChatRoute,
  type AccountCloudAgentSummary,
  type CloudDeploymentSummary,
} from "../cloudDeployments";
import { deploymentDetailRoute, navigateToStudioHash } from "../studioRoutes";

interface Deployment {
  id: string;
  buildId: string;
  bundleDigest: string;
  versionId: string;
  status: "ADMITTING" | "DEPLOYING" | "READY" | "FAILED" | "ROLLED_BACK" | string;
  target: { region: string; environment: string };
  agentId?: string;
  instanceId?: string;
  endpoint?: string;
  artifactId?: string;
  source?: "receipt" | "account";
  agentName?: string;
  framework?: string;
  runtimeType?: string;
  capabilities?: Record<string, unknown>;
  chatTransport?: "studio-session-events" | "official-dashboard";
  chatRoutingReason?:
    | "declared-session-event-chat-capability"
    | "native-runtime-without-session-event-chat-capability"
    | "studio-compatible-framework";
  updatedAt?: string;
}

interface StudioCloudAgentSummary extends AccountCloudAgentSummary {
  region?: string;
}

interface BuildCandidate {
  id: string;
  agentId?: string;
  agentName?: string;
  status: string;
  bundleDigest?: string;
  createdAt?: string;
  runtimeName?: string;
  runtimeVersion?: string;
  artifactType?: string;
}

interface CloudVersion {
  versionId: string;
  versionName: string;
  tag: string;
  status: string;
  trafficPercentage: number;
  createdAt?: string;
  createdBy?: string;
  canRollback: boolean;
  rollbackDisabledReason: string;
}

interface DeploymentDetail {
  deployment: Deployment;
  sourceAgentId: string;
  sourceAgentName: string;
  builds: BuildCandidate[];
  versions: CloudVersion[];
  currentVersionId: string;
  loading: boolean;
  error: string;
}

interface DeploymentCreateSelection {
  buildId: string;
  agentId: string;
}

interface CloudVersionCatalog {
  items: CloudVersion[];
  currentVersionId: string;
}

const OPERATION_TERMINAL = new Set(["SUCCEEDED", "FAILED", "CANCELLED", "TIMED_OUT", "INTERRUPTED"]);
const OPERATION_ATTEMPTS_STORAGE_PREFIX = "agentkit-studio:deployment-operation-attempts:v2";
const OPERATION_POLL_INTERVAL_MS = 500;
const STALE_OPERATION_STATUSES = new Set([403, 404, 410]);
const localOperationLocks = new Map<string, Promise<void>>();
let volatileOperationAttempts: Record<string, Record<string, OperationAttempt>> = {};

interface OperationResult {
  status: string;
  resourceId?: string;
  error?: { message?: string };
}

interface OperationAttempt {
  actionKey: string;
  idempotencyKey: string;
  operationId: string;
}

interface DeploymentOperationScope {
  workspace: string;
  cloudCredential: string;
}

class OperationStatusError extends Error {
  constructor(message: string, readonly status: number) {
    super(message);
    this.name = "OperationStatusError";
  }
}

function operationStorage(): Storage | undefined {
  try {
    return typeof document === "undefined" ? undefined : document.defaultView?.localStorage;
  } catch {
    return undefined;
  }
}

function apiFetchWithSignal(path: string, signal?: AbortSignal): Promise<Response> {
  return signal ? apiFetch(path, { signal }) : apiFetch(path);
}

function mergeCloudProjection(
  deployment: Deployment,
  account?: Partial<StudioCloudAgentSummary> | null,
): Deployment {
  if (!account) return deployment;
  return {
    ...deployment,
    status: String(account.status || deployment.status).toUpperCase(),
    agentName: account.name || deployment.agentName,
    endpoint: account.endpoint || deployment.endpoint,
    framework: account.framework || deployment.framework,
    runtimeType: account.runtimeType || deployment.runtimeType,
    capabilities: account.capabilities || deployment.capabilities,
    chatTransport: account.chatTransport || deployment.chatTransport,
    chatRoutingReason: account.chatRoutingReason || deployment.chatRoutingReason,
    versionId: account.versionId || deployment.versionId,
    updatedAt: account.updatedAt || deployment.updatedAt,
  };
}

function readOperationAttempts(storageKey: string): Record<string, OperationAttempt> {
  try {
    const storage = operationStorage();
    if (!storage) return { ...(volatileOperationAttempts[storageKey] || {}) };
    const parsed = JSON.parse(storage.getItem(storageKey) || "{}");
    return parsed && typeof parsed === "object" ? parsed : {};
  } catch {
    return { ...(volatileOperationAttempts[storageKey] || {}) };
  }
}

function writeOperationAttempts(storageKey: string, attempts: Record<string, OperationAttempt>) {
  volatileOperationAttempts = {
    ...volatileOperationAttempts,
    [storageKey]: { ...attempts },
  };
  try {
    operationStorage()?.setItem(storageKey, JSON.stringify(attempts));
  } catch {
    // A disabled storage backend must not prevent lifecycle operations.
  }
}

function newIdempotencyKey(kind: string): string {
  const randomId = globalThis.crypto?.randomUUID?.()
    || `${Math.random().toString(36).slice(2)}-${Date.now().toString(36)}`;
  return `studio-${kind}-${randomId}`;
}

function acquireOperationAttempt(storageKey: string, actionKey: string, kind: string): OperationAttempt {
  const attempts = readOperationAttempts(storageKey);
  const existing = attempts[actionKey];
  if (existing?.idempotencyKey) return existing;
  const attempt = { actionKey, idempotencyKey: newIdempotencyKey(kind), operationId: "" };
  attempts[actionKey] = attempt;
  writeOperationAttempts(storageKey, attempts);
  return attempt;
}

function persistOperationId(storageKey: string, attempt: OperationAttempt, operationId: string): OperationAttempt {
  const updated = { ...attempt, operationId };
  const attempts = readOperationAttempts(storageKey);
  attempts[attempt.actionKey] = updated;
  writeOperationAttempts(storageKey, attempts);
  return updated;
}

function clearOperationAttempt(storageKey: string, attempt: OperationAttempt) {
  const attempts = readOperationAttempts(storageKey);
  const current = attempts[attempt.actionKey];
  if (!current || current.idempotencyKey !== attempt.idempotencyKey) return;
  delete attempts[attempt.actionKey];
  writeOperationAttempts(storageKey, attempts);
}

function isAbortError(error: unknown): boolean {
  return error instanceof DOMException && error.name === "AbortError";
}

function throwIfAborted(signal: AbortSignal) {
  if (signal.aborted) throw new DOMException("Operation aborted", "AbortError");
}

async function abortableDelay(milliseconds: number, signal: AbortSignal): Promise<void> {
  throwIfAborted(signal);
  await new Promise<void>((resolve, reject) => {
    const timer = window.setTimeout(() => {
      signal.removeEventListener("abort", abort);
      resolve();
    }, milliseconds);
    const abort = () => {
      window.clearTimeout(timer);
      reject(new DOMException("Operation aborted", "AbortError"));
    };
    signal.addEventListener("abort", abort, { once: true });
  });
}

async function withLocalOperationLock<T>(name: string, callback: () => Promise<T>): Promise<T> {
  const previous = localOperationLocks.get(name) || Promise.resolve();
  let release!: () => void;
  const current = new Promise<void>(resolve => { release = resolve; });
  const queued = previous.then(() => current);
  localOperationLocks.set(name, queued);
  await previous;
  try {
    return await callback();
  } finally {
    release();
    if (localOperationLocks.get(name) === queued) localOperationLocks.delete(name);
  }
}

async function withOperationLock<T>(
  name: string,
  signal: AbortSignal,
  callback: () => Promise<T>,
): Promise<T> {
  throwIfAborted(signal);
  const manager = navigator.locks;
  if (manager) {
    return manager.request(name, { mode: "exclusive", signal }, async () => callback());
  }
  return withLocalOperationLock(name, async () => {
    throwIfAborted(signal);
    return callback();
  });
}

async function operationStorageKey(signal: AbortSignal): Promise<string> {
  const response = await apiFetch("/api/v1/system/bootstrap", { signal });
  if (!response.ok) throw new Error(`读取部署操作作用域失败（${response.status}）`);
  const payload = await response.json();
  const scope = payload?.operationScope as DeploymentOperationScope | undefined;
  const workspace = String(scope?.workspace || "").trim();
  const cloudCredential = String(scope?.cloudCredential || "").trim();
  if (!workspace || !cloudCredential) throw new Error("部署操作作用域不可用，请刷新 Studio");
  return `${OPERATION_ATTEMPTS_STORAGE_PREFIX}:${encodeURIComponent(workspace)}:${encodeURIComponent(cloudCredential)}`;
}

async function waitForOperation(
  operationId: string,
  operationLabel: string,
  signal: AbortSignal,
): Promise<OperationResult> {
  while (true) {
    await abortableDelay(OPERATION_POLL_INTERVAL_MS, signal);
    const response = await apiFetch(
      `/api/v1/operations/${encodeURIComponent(operationId)}`,
      { signal },
    );
    if (!response.ok) {
      throw new OperationStatusError(`${operationLabel}状态读取失败（${response.status}）`, response.status);
    }
    const result = await response.json() as OperationResult;
    if (OPERATION_TERMINAL.has(result.status)) return result;
  }
}

async function submitOrResumeOperation(
  storageKey: string,
  actionKey: string,
  kind: string,
  operationLabel: string,
  signal: AbortSignal,
  submit: (idempotencyKey: string, signal: AbortSignal) => Promise<Response>,
): Promise<OperationResult> {
  const lockName = `${storageKey}:${actionKey}`;
  const attempt = await withOperationLock(lockName, signal, async () => {
    let current = acquireOperationAttempt(storageKey, actionKey, kind);
    if (!current.operationId) {
      const response = await submit(current.idempotencyKey, signal);
      if (!response.ok) {
        // 5xx may be an ambiguous response after the Server accepted the write.
        // Keep its key so the next retry asks the Server for the same operation.
        if (response.status < 500) clearOperationAttempt(storageKey, current);
        throw new Error(`${operationLabel}提交失败（${response.status}）`);
      }
      const operation = await response.json();
      const operationId = String(operation?.id || "").trim();
      if (!operationId) {
        clearOperationAttempt(storageKey, current);
        throw new Error(`${operationLabel}提交结果缺少 operation_id`);
      }
      current = persistOperationId(storageKey, current, operationId);
    }
    return current;
  });

  let result: OperationResult;
  try {
    result = await waitForOperation(attempt.operationId, operationLabel, signal);
  } catch (error) {
    if (error instanceof OperationStatusError && STALE_OPERATION_STATUSES.has(error.status)) {
      await withOperationLock(lockName, signal, async () => {
        clearOperationAttempt(storageKey, attempt);
      });
    }
    throw error;
  }
  await withOperationLock(lockName, signal, async () => {
    clearOperationAttempt(storageKey, attempt);
  });
  return result;
}

function deploymentState(status: string): "ready" | "failed" | "pending" | "idle" {
  if (["READY", "RUNNING"].includes(status)) return "ready";
  if (["FAILED", "ROLLED_BACK", "ERROR", "TERMINATED"].includes(status)) return "failed";
  if (["ADMITTING", "DEPLOYING", "CREATING", "UPDATING"].includes(status)) return "pending";
  return "idle";
}

function deploymentLabel(status: string): string {
  return ({
    ADMITTING: "准入中",
    DEPLOYING: "部署中",
    READY: "已就绪",
    FAILED: "部署失败",
    ROLLED_BACK: "已回滚",
    RUNNING: "运行中",
    CREATING: "创建中",
    UPDATING: "更新中",
    ERROR: "异常",
    TERMINATED: "已终止",
  } as Record<string, string>)[status] || "状态未知";
}

function shortId(value: string, max = 28): string {
  return value.length > max ? `${value.slice(0, max)}…` : value;
}

function deploymentRouteId(): string {
  const match = window.location.hash.match(/^#\/deployments\/([^/?]+)(?:\?.*)?$/);
  const routeId = match ? decodeURIComponent(match[1]) : "";
  return routeId === "new" ? "" : routeId;
}

function deploymentCreateSelection(): DeploymentCreateSelection | null {
  const match = window.location.hash.match(/^#\/deployments\/new(?:\?(.*))?$/);
  if (!match) return null;
  const params = new URLSearchParams(match[1] || "");
  return {
    buildId: params.get("buildId")?.trim() || "",
    agentId: params.get("agentId")?.trim() || "",
  };
}

function formatUpdatedAt(value?: string): string {
  if (!value) return "—";
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString("zh-CN", { hour12: false });
}

export function DeploymentsPage({ onCreate, onOpenChat, onSelectBuild }: {
  onCreate: () => void;
  onOpenChat: (deploymentId: string) => void;
  onSelectBuild: () => void;
}) {
  const [deployments, setDeployments] = useState<Deployment[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [refreshing, setRefreshing] = useState<Set<string>>(new Set());
  const [selectedRollbackVersionId, setSelectedRollbackVersionId] = useState("");
  const [rollbackConfirmOpen, setRollbackConfirmOpen] = useState(false);
  const [rollbackBusy, setRollbackBusy] = useState(false);
  const [detail, setDetail] = useState<DeploymentDetail | null>(null);
  const [updating, setUpdating] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState<Deployment | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [createSelection, setCreateSelection] = useState<DeploymentCreateSelection | null>(() => deploymentCreateSelection());
  const [deployableBuilds, setDeployableBuilds] = useState<BuildCandidate[]>([]);
  const [selectedBuildId, setSelectedBuildId] = useState(() => deploymentCreateSelection()?.buildId || "");
  const [cloudRegion, setCloudRegion] = useState("");
  const [createLoading, setCreateLoading] = useState(false);
  const [createBusy, setCreateBusy] = useState(false);
  const [createError, setCreateError] = useState("");
  const operationControllers = useRef(new Set<AbortController>());

  useEffect(() => () => {
    for (const controller of operationControllers.current) controller.abort();
    operationControllers.current.clear();
  }, []);

  function beginOperation(): AbortController {
    const controller = new AbortController();
    operationControllers.current.add(controller);
    return controller;
  }

  function finishOperation(controller: AbortController) {
    operationControllers.current.delete(controller);
  }

  const load = useCallback(async (signal?: AbortSignal) => {
    setLoading(true);
    setError("");
    try {
      const [receiptResponse, accountResponse] = await Promise.all([
        apiFetchWithSignal("/api/v1/deployments", signal),
        apiFetchWithSignal("/api/v1/cloud-agents?size=100", signal),
      ]);
      if (!receiptResponse.ok) throw new Error(`读取部署记录失败（${receiptResponse.status}）`);
      const receiptPayload = await receiptResponse.json();
      const accountPayload = accountResponse.ok ? await accountResponse.json() : { items: [] };
      const receipts: Deployment[] = Array.isArray(receiptPayload.items) ? receiptPayload.items : [];
      const receiptAgentIds = [...new Set(receipts.flatMap(item => (
        item.agentId?.trim() ? [item.agentId.trim()] : []
      )))];
      const accountDetails: Array<StudioCloudAgentSummary | null> = await Promise.all(receiptAgentIds.map(agentId => (
        Promise.resolve()
          .then(() => apiFetchWithSignal(
            `/api/v1/cloud-agents/${encodeURIComponent(agentId)}`,
            signal,
          ))
          .then(async response => response.ok ? await response.json() as StudioCloudAgentSummary : null)
          .catch(error => {
            if (isAbortError(error)) throw error;
            return null;
          })
      )));
      const listedAccounts: StudioCloudAgentSummary[] = Array.isArray(accountPayload.items)
        ? accountPayload.items as StudioCloudAgentSummary[]
        : [];
      const accountByAgentId = new Map<string, StudioCloudAgentSummary>(listedAccounts.flatMap(item => (
        item.agentId ? [[item.agentId, item] as const] : []
      )));
      for (const detail of accountDetails) {
        if (detail?.agentId) accountByAgentId.set(detail.agentId, { ...accountByAgentId.get(detail.agentId), ...detail });
      }
      const accountItems = [...accountByAgentId.values()];
      const receiptById = new Map(receipts.map(item => [item.id, item]));
      const targets = mergeCloudChatTargets(receipts as CloudDeploymentSummary[], accountItems);
      const rows = targets.map(target => {
        if (target.source === "receipt") {
          return {
            ...receiptById.get(target.id)!,
            ...target,
            id: target.id,
            source: "receipt" as const,
          };
        }
        const account = accountItems.find(item => item.agentId === target.agentId);
        return {
          id: target.id,
          buildId: "",
          bundleDigest: "",
          versionId: String(target.versionId || account?.versionId || ""),
          status: String(target.status || account?.status || "UNKNOWN").toUpperCase(),
          target: { region: String(account?.region || ""), environment: "cloud" },
          agentId: target.agentId,
          agentName: target.agentName,
          endpoint: target.endpoint,
          framework: String(target.framework || account?.framework || ""),
          runtimeType: String(target.runtimeType || account?.runtimeType || ""),
          capabilities: target.capabilities || account?.capabilities,
          chatTransport: target.chatTransport || account?.chatTransport,
          chatRoutingReason: target.chatRoutingReason || account?.chatRoutingReason,
          updatedAt: String(target.updatedAt || account?.updatedAt || ""),
          source: "account" as const,
        };
      });
      if (!signal?.aborted) setDeployments(rows);
    } catch (caught: any) {
      if (!isAbortError(caught) && !signal?.aborted) setError(caught?.message || "部署记录不可用");
    } finally {
      if (!signal?.aborted) setLoading(false);
    }
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    void load(controller.signal);
    return () => controller.abort();
  }, [load]);

  useEffect(() => {
    const syncDetailRoute = () => {
      const create = deploymentCreateSelection();
      setCreateSelection(create);
      if (create) {
        setSelectedBuildId(create.buildId);
        setDetail(null);
        return;
      }
      const routeId = deploymentRouteId();
      if (!routeId) {
        setDetail(null);
        return;
      }
      const deployment = deployments.find(item => item.id === routeId);
      if (deployment && detail?.deployment.id !== routeId) void openDetail(deployment, false);
    };
    syncDetailRoute();
    window.addEventListener("popstate", syncDetailRoute);
    window.addEventListener("hashchange", syncDetailRoute);
    return () => {
      window.removeEventListener("popstate", syncDetailRoute);
      window.removeEventListener("hashchange", syncDetailRoute);
    };
  }, [deployments, detail?.deployment.id]);

  useEffect(() => {
    if (!createSelection) return;
    let cancelled = false;
    setCreateLoading(true);
    setCreateError("");

    void (async () => {
      try {
        const [agentsResponse, settingsResponse] = await Promise.all([
          apiFetch("/api/v1/agents?limit=100"),
          apiFetch("/api/v1/system/settings"),
        ]);
        if (!agentsResponse.ok) throw new Error(`读取可部署 Build 失败（${agentsResponse.status}）`);
        const agentPayload = await agentsResponse.json();
        const settings = settingsResponse.ok ? await settingsResponse.json() : {};
        const agents = Array.isArray(agentPayload.items) ? agentPayload.items : [];
        const details = await Promise.all(agents.map(async (summary: any) => {
          const agentId = String(summary?.metadata?.id || "").trim();
          if (!agentId) return null;
          const response = await apiFetch(`/api/v1/agents/${encodeURIComponent(agentId)}`);
          if (!response.ok) return null;
          return response.json();
        }));
        let candidates = details.flatMap((agent: any) => {
          if (!agent) return [];
          const agentId = String(agent?.draft?.metadata?.id || "").trim();
          const agentName = String(agent?.draft?.metadata?.name || agentId || "未命名 Agent");
          const runtimeType = String(agent?.draft?.spec?.runtime?.type || "");
          const artifactType = String(
            agent?.draft?.metadata?.labels?.["agentkit.ksyun.com/artifact-type"]
              || (runtimeType === "codex" ? "ManagedRuntime" : "Code"),
          );
          return (Array.isArray(agent.builds) ? agent.builds : [])
            .filter((build: BuildCandidate) => build.status === "SUCCEEDED")
            .map((build: BuildCandidate) => ({
              ...build,
              agentId,
              agentName,
              runtimeName: build.runtimeName || runtimeType,
              artifactType,
            }));
        }).sort((left: BuildCandidate, right: BuildCandidate) => (
          String(right.createdAt || "").localeCompare(String(left.createdAt || ""))
        ));
        if (createSelection.buildId && !candidates.some(build => build.id === createSelection.buildId)) {
          const buildResponse = await apiFetch(`/api/v1/builds/${encodeURIComponent(createSelection.buildId)}`);
          if (buildResponse.ok) {
            const build = await buildResponse.json();
            const agentId = String(build.agentId || createSelection.agentId || "").trim();
            const agentResponse = agentId
              ? await apiFetch(`/api/v1/agents/${encodeURIComponent(agentId)}`)
              : null;
            const agent = agentResponse?.ok ? await agentResponse.json() : null;
            if (String(build.status || "") === "SUCCEEDED" && agentId) {
              const runtimeType = String(agent?.draft?.spec?.runtime?.type || build.runtimeName || "");
              candidates = [{
                ...build,
                agentId,
                agentName: String(agent?.draft?.metadata?.name || agentId),
                runtimeName: build.runtimeName || runtimeType,
                artifactType: String(
                  agent?.draft?.metadata?.labels?.["agentkit.ksyun.com/artifact-type"]
                    || (runtimeType === "codex" ? "ManagedRuntime" : "Code"),
                ),
              }, ...candidates];
            }
          }
        }
        if (cancelled) return;
        setCloudRegion(String(settings.cloudRegion || "").trim());
        setDeployableBuilds(candidates);
        if (createSelection.buildId) {
          if (!candidates.some((build: BuildCandidate) => build.id === createSelection.buildId)) {
            throw new Error(`Build ${createSelection.buildId} 不存在或尚未成功`);
          }
          setSelectedBuildId(createSelection.buildId);
        } else {
          setSelectedBuildId(current => candidates.some((build: BuildCandidate) => build.id === current) ? current : "");
        }
      } catch (caught: any) {
        if (!cancelled) setCreateError(caught?.message || "可部署 Build 不可用");
      } finally {
        if (!cancelled) setCreateLoading(false);
      }
    })();
    return () => { cancelled = true; };
  }, [createSelection?.agentId, createSelection?.buildId]);

  const summary = useMemo(() => ({
    ready: deployments.filter(item => deploymentState(item.status) === "ready").length,
    pending: deployments.filter(item => deploymentState(item.status) === "pending").length,
    failed: deployments.filter(item => deploymentState(item.status) === "failed").length,
  }), [deployments]);

  async function refresh(deployment: Deployment) {
    setRefreshing(current => new Set(current).add(deployment.id));
    try {
      const response = deployment.source === "account"
        ? await apiFetch(`/api/v1/cloud-agents/${encodeURIComponent(deployment.agentId || "")}`)
        : await apiFetch(`/api/v1/deployments/${encodeURIComponent(deployment.id)}`);
      if (!response.ok) throw new Error(`状态刷新失败（${response.status}）`);
      const updated = await response.json();
      let next = deployment.source === "account"
        ? mergeCloudProjection(deployment, updated)
        : { ...deployment, ...updated, source: "receipt" as const };
      if (deployment.source === "receipt" && next.agentId) {
        const accountResponse = await apiFetch(
          `/api/v1/cloud-agents/${encodeURIComponent(next.agentId)}`,
        ).catch(() => null);
        if (accountResponse?.ok) {
          next = mergeCloudProjection(next, await accountResponse.json());
        }
      }
      setDeployments(current => current.map(item => item.id === deployment.id ? next : item));
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

  async function openHostedUi(deployment: Deployment) {
    setError("");
    try {
      const chatRoute = resolveCloudChatRoute(deployment);
      const dashboardPath = chatRoute.kind === "official-dashboard" || deployment.source === "account"
        ? `/api/v1/cloud-agents/${encodeURIComponent(deployment.agentId || "")}:dashboard`
        : `/api/v1/deployments/${encodeURIComponent(deployment.id)}:dashboard`;
      const response = await apiFetch(
        dashboardPath,
        { method: "POST" },
      );
      if (!response.ok) throw new Error(`创建云端 UI 访问链接失败（${response.status}）`);
      const payload = await response.json();
      const accessUrl = String(payload?.accessUrl || payload?.access_url || "").trim();
      if (!accessUrl) throw new Error("云端未返回 Agent UI 地址");
      window.open(accessUrl, "_blank", "noopener,noreferrer");
    } catch (caught: any) {
      setError(`${deployment.instanceId || deployment.id}：${caught?.message || "无法打开云端 UI"}`);
    }
  }

  async function loadCloudVersions(agentId: string, signal?: AbortSignal): Promise<CloudVersionCatalog> {
    const response = await apiFetchWithSignal(
      `/api/v1/cloud-agents/${encodeURIComponent(agentId)}/versions?page=1&size=100`,
      signal,
    );
    if (!response.ok) throw new Error(`读取云端版本失败（${response.status}）`);
    const payload = await response.json();
    const rows = payload.items || payload.versions || payload.Versions || [];
    const items = (Array.isArray(rows) ? rows : []).map((item: any) => ({
      versionId: String(item.versionId || item.version_id || item.VersionId || ""),
      versionName: String(item.versionName || item.version_name || item.VersionName || ""),
      tag: String(item.tag || item.Tag || ""),
      status: String(item.status || item.Status || ""),
      trafficPercentage: Number(item.trafficPercentage ?? item.traffic_percentage ?? item.TrafficPercentage ?? 0),
      createdAt: item.createdAt || item.created_at || item.CreatedAt,
      createdBy: item.createdBy || item.created_by || item.CreatedBy,
      canRollback: Boolean(item.canRollback ?? item.can_rollback ?? item.CanRollback),
      rollbackDisabledReason: String(
        item.rollbackDisabledReason || item.rollback_disabled_reason || item.RollbackDisabledReason || "",
      ),
    })).filter((item: CloudVersion) => item.versionId);
    return {
      items,
      currentVersionId: String(
        payload.currentVersionId || payload.current_version_id || payload.CurrentVersionId || "",
      ),
    };
  }

  async function openDetail(deployment: Deployment, navigate = true, signal?: AbortSignal) {
    if (signal?.aborted) return;
    if (navigate) {
      window.history.pushState(null, "", `#/deployments/${encodeURIComponent(deployment.id)}`);
    }
    setDetail({ deployment, sourceAgentId: "", sourceAgentName: "", builds: [], versions: [], currentVersionId: "", loading: true, error: "" });
    setSelectedRollbackVersionId("");
    setRollbackConfirmOpen(false);
    const versionsPromise = deployment.agentId
      ? loadCloudVersions(deployment.agentId, signal)
      : Promise.resolve({ items: [], currentVersionId: "" } as CloudVersionCatalog);
    try {
      if (deployment.source === "account") {
        const accountResponse = await apiFetchWithSignal(
          `/api/v1/cloud-agents/${encodeURIComponent(deployment.agentId || "")}`,
          signal,
        );
        if (!accountResponse.ok) throw new Error(`刷新云端 Agent 状态失败（${accountResponse.status}）`);
        const account = await accountResponse.json();
        const refreshed = {
          ...deployment,
          agentName: String(account.name || deployment.agentName || deployment.agentId || "云端 Agent"),
          status: String(account.status || deployment.status).toUpperCase(),
          endpoint: account.endpoint || deployment.endpoint,
          framework: account.framework || deployment.framework,
          runtimeType: account.runtimeType || deployment.runtimeType,
          capabilities: account.capabilities || deployment.capabilities,
          chatTransport: account.chatTransport || deployment.chatTransport,
          chatRoutingReason: account.chatRoutingReason || deployment.chatRoutingReason,
          versionId: account.versionId || deployment.versionId,
          updatedAt: account.updatedAt || deployment.updatedAt,
        };
        const versions = await versionsPromise;
        if (signal?.aborted) return;
        setDeployments(current => current.map(item => item.id === deployment.id ? refreshed : item));
        setDetail({
          deployment: refreshed,
          sourceAgentId: "",
          sourceAgentName: refreshed.agentName || refreshed.agentId || "账号云端 Agent",
          builds: [],
          versions: versions.items,
          currentVersionId: versions.currentVersionId,
          loading: false,
          error: "",
        });
        return;
      }
      const deploymentResponse = await apiFetchWithSignal(
        `/api/v1/deployments/${encodeURIComponent(deployment.id)}`,
        signal,
      );
      if (!deploymentResponse.ok) throw new Error(`刷新云端 Agent 状态失败（${deploymentResponse.status}）`);
      const refreshed: Deployment = {
        ...deployment,
        ...(await deploymentResponse.json() as Deployment),
        source: "receipt" as const,
      };
      let projected = refreshed;
      if (refreshed.agentId) {
        const accountResponse = await apiFetchWithSignal(
          `/api/v1/cloud-agents/${encodeURIComponent(refreshed.agentId)}`,
          signal,
        ).catch(error => {
          if (isAbortError(error)) throw error;
          return null;
        });
        if (accountResponse?.ok) {
          projected = mergeCloudProjection(refreshed, await accountResponse.json());
        }
      }
      if (signal?.aborted) return;
      setDeployments(current => current.map(item => item.id === deployment.id ? projected : item));
      const buildResponse = await apiFetchWithSignal(
        `/api/v1/builds/${encodeURIComponent(projected.buildId)}`,
        signal,
      );
      if (!buildResponse.ok) throw new Error(`读取当前 Build 失败（${buildResponse.status}）`);
      const build = await buildResponse.json();
      const sourceAgentId = String(build.agentId || "").trim();
      if (!sourceAgentId) throw new Error("当前部署缺少本地 Agent 关联");
      const agentResponse = await apiFetchWithSignal(
        `/api/v1/agents/${encodeURIComponent(sourceAgentId)}`,
        signal,
      );
      if (!agentResponse.ok) throw new Error(`读取本地 Build 历史失败（${agentResponse.status}）`);
      const agent = await agentResponse.json();
      const builds = (Array.isArray(agent.builds) ? agent.builds : [])
        .filter((item: BuildCandidate) => item.status === "SUCCEEDED")
        .sort((left: BuildCandidate, right: BuildCandidate) => String(right.createdAt || "").localeCompare(String(left.createdAt || "")));
      const versions = await versionsPromise;
      if (signal?.aborted) return;
      setDetail({
        deployment: projected,
        sourceAgentId,
        sourceAgentName: String(agent?.draft?.metadata?.name || sourceAgentId),
        builds,
        versions: versions.items,
        currentVersionId: versions.currentVersionId,
        loading: false,
        error: "",
      });
    } catch (caught: any) {
      if (isAbortError(caught) || signal?.aborted) return;
      const versions = await versionsPromise.catch(() => ({ items: [], currentVersionId: "" }));
      setDetail(current => current ? {
        ...current,
        versions: versions.items,
        currentVersionId: versions.currentVersionId,
        loading: false,
        error: caught?.message || "云端 Agent 详情不可用",
      } : null);
    }
  }

  async function submitDeployment() {
    if (!selectedBuildId || createBusy) return;
    const selectedBuild = deployableBuilds.find(build => build.id === selectedBuildId);
    if (!selectedBuild) {
      setCreateError("请选择一个已成功的 Build");
      return;
    }
    if (!cloudRegion) {
      setCreateError("请先在 Studio 设置中配置云端 Region");
      return;
    }
    setCreateBusy(true);
    setCreateError("");
    const actionKey = `deploy:${selectedBuildId}:${cloudRegion}`;
    const controller = beginOperation();
    try {
      const storageKey = await operationStorageKey(controller.signal);
      const result = await submitOrResumeOperation(storageKey, actionKey, "deploy", "部署", controller.signal, (idempotencyKey, signal) => (
        apiFetch(`/api/v1/builds/${encodeURIComponent(selectedBuildId)}/deployments`, {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "Idempotency-Key": idempotencyKey,
          },
          body: JSON.stringify({
            target: { region: cloudRegion, environment: "cloud" },
            releasePolicy: { strategy: "rolling", approval: "none" },
          }),
          signal,
        })
      ));
      if (result.status !== "SUCCEEDED") throw new Error(result.error?.message || "部署未完成");
      throwIfAborted(controller.signal);
      await load(controller.signal);
      throwIfAborted(controller.signal);
      const deploymentId = String(result.resourceId || "").trim();
      navigateToStudioHash(deploymentId ? deploymentDetailRoute(deploymentId) : "#/deployments");
      showToast("已提交云端部署", `${selectedBuild.agentName || selectedBuild.agentId} · ${selectedBuild.id}`);
    } catch (caught: any) {
      if (!isAbortError(caught) && !controller.signal.aborted) {
        setCreateError(caught?.message || "部署失败");
      }
    } finally {
      finishOperation(controller);
      if (!controller.signal.aborted) setCreateBusy(false);
    }
  }

  function closeDetail() {
    setDetail(null);
    setSelectedRollbackVersionId("");
    setRollbackConfirmOpen(false);
    window.history.pushState(null, "", "#/deployments");
  }

  async function updateToBuild(deployment: Deployment, buildId: string) {
    if (!buildId || updating) return;
    setUpdating(true);
    setError("");
    const actionKey = `update:${deployment.agentId || deployment.id}:${buildId}`;
    const controller = beginOperation();
    try {
      const storageKey = await operationStorageKey(controller.signal);
      const result = await submitOrResumeOperation(storageKey, actionKey, "update", "更新", controller.signal, (idempotencyKey, signal) => (
        apiFetch(`/api/v1/builds/${encodeURIComponent(buildId)}/deployments`, {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "Idempotency-Key": idempotencyKey,
          },
          body: JSON.stringify({
            target: deployment.target,
            releasePolicy: { strategy: "rolling", approval: "none" },
          }),
          signal,
        })
      ));
      if (result.status !== "SUCCEEDED") throw new Error(result.error?.message || "更新未完成");
      throwIfAborted(controller.signal);
      setDetail(null);
      await load(controller.signal);
      throwIfAborted(controller.signal);
      showToast("已提交云端更新", `Build ${buildId}`);
    } catch (caught: any) {
      if (!isAbortError(caught) && !controller.signal.aborted) setError(caught?.message || "更新失败");
    } finally {
      finishOperation(controller);
      if (!controller.signal.aborted) setUpdating(false);
    }
  }

  async function deleteCloudAgent() {
    if (!deleteTarget || deleting) return;
    const target = deleteTarget;
    setDeleting(true);
    setError("");
    try {
      const deletePath = target.source === "account"
        ? `/api/v1/cloud-agents/${encodeURIComponent(target.agentId || "")}`
        : `/api/v1/deployments/${encodeURIComponent(target.id)}`;
      const response = await apiFetch(deletePath, { method: "DELETE" });
      if (!response.ok) throw new Error(`删除云端 Agent 失败（${response.status}）`);
      const result = await response.json();
      setDeleteTarget(null);
      setDetail(null);
      await load();
      showToast("云端 Agent 已删除", String(result.agentId || target.agentId || ""));
    } catch (caught: any) {
      setError(caught?.message || "删除云端 Agent 失败");
    } finally {
      setDeleting(false);
    }
  }

  async function submitRollback() {
    if (!detail || !selectedRollbackVersionId || rollbackBusy) return;
    const deployment = detail.deployment;
    const agentId = String(deployment.agentId || "").trim();
    const targetVersionId = selectedRollbackVersionId;
    const targetVersion = detail.versions.find(version => version.versionId === targetVersionId);
    const isCurrent = targetVersionId === detail.currentVersionId
      || targetVersion?.status.toLowerCase() === "current";
    if (!agentId || !targetVersion?.canRollback || isCurrent) return;
    setRollbackBusy(true);
    setDetail(current => current ? { ...current, error: "" } : current);
    const actionKey = `rollback:${agentId}:${targetVersionId}`;
    const controller = beginOperation();
    try {
      const storageKey = await operationStorageKey(controller.signal);
      const result = await submitOrResumeOperation(storageKey, actionKey, "rollback", "回滚", controller.signal, (idempotencyKey, signal) => (
        apiFetch(`/api/v1/cloud-agents/${encodeURIComponent(agentId)}:rollback-version`, {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "Idempotency-Key": idempotencyKey,
          },
          body: JSON.stringify({ versionId: targetVersionId }),
          signal,
        })
      ));
      if (result.status !== "SUCCEEDED") throw new Error(result.error?.message || "回滚未完成");
      throwIfAborted(controller.signal);
      setRollbackConfirmOpen(false);
      setSelectedRollbackVersionId("");
      await load(controller.signal);
      await openDetail(deployment, false, controller.signal);
      throwIfAborted(controller.signal);
      showToast("已提交版本回滚", `云端版本 ${targetVersionId}`);
    } catch (caught: any) {
      if (!isAbortError(caught) && !controller.signal.aborted) {
        setRollbackConfirmOpen(false);
        setDetail(current => current ? { ...current, error: caught?.message || "回滚失败" } : current);
      }
    } finally {
      finishOperation(controller);
      if (!controller.signal.aborted) setRollbackBusy(false);
    }
  }

  if (createSelection) {
    const selectedBuild = deployableBuilds.find(build => build.id === selectedBuildId);
    return (
      <div className="delivery-page deployment-create-page" data-layout="document">
        <PageHeaderActions>
          <button className="button secondary" type="button" onClick={() => navigateToStudioHash("#/deployments")}>
            <ArrowLeft size={15} /><span>返回云端 Agent</span>
          </button>
          <button className="button accent" type="button" onClick={() => void submitDeployment()} disabled={!selectedBuild || createBusy || createLoading}>
            <CloudUpload size={15} /><span>{createBusy ? "部署中…" : "部署到云端"}</span>
          </button>
        </PageHeaderActions>
        <div className="delivery-intro">
          <div><h2>部署到云端</h2><p>选择一个已成功的 Build，由 Studio 提交统一云端部署操作。</p></div>
        </div>
        {createError && <div className="form-error" role="alert">{createError}</div>}
        <section className="delivery-block" aria-label="选择部署 Build">
          <h2>选择 Build</h2><p>ManagedRuntime 使用已校验声明；ADK、LangGraph 等代码 Agent 使用不可变 Code Bundle。</p>
          {createLoading ? <p>正在读取可部署 Build…</p> : deployableBuilds.length ? (
            <div className="deployment-version-list" role="radiogroup" aria-label="可部署 Build">
              {deployableBuilds.map(build => (
                <button
                  key={build.id}
                  type="button"
                  role="radio"
                  aria-checked={build.id === selectedBuildId}
                  aria-label={`${build.agentName || build.agentId || "Agent"} ${build.id}`}
                  className="deployment-version-option"
                  data-selected={build.id === selectedBuildId}
                  onClick={() => setSelectedBuildId(build.id)}
                >
                  <strong className="deployment-version-name">{build.agentName || build.agentId || "未命名 Agent"}</strong>
                  <span className="deployment-version-state" data-state="available">{build.artifactType === "ManagedRuntime" ? "托管声明" : "代码 Bundle"}</span>
                  <code title={build.id}>{shortId(build.id, 24)}</code>
                  <time className="deployment-version-time" dateTime={build.createdAt || undefined}>{formatUpdatedAt(build.createdAt)}</time>
                </button>
              ))}
            </div>
          ) : (
            <div className="delivery-empty-state">
              <Package size={24} /><h2>没有可部署的 Build</h2>
              <p>请先完成 Agent 构建或 ManagedRuntime 声明校验。</p>
              <button className="button secondary" type="button" onClick={onSelectBuild}>前往构建</button>
            </div>
          )}
          {selectedBuild && (
            <div className="api-contract" aria-label="部署提交摘要">
              <div><span>Agent</span><strong>{selectedBuild.agentName || selectedBuild.agentId}</strong></div>
              <div><span>Build</span><code>{selectedBuild.id}</code></div>
              <div><span>制品</span><strong>{selectedBuild.artifactType === "ManagedRuntime" ? "ManagedRuntime 声明" : "Code Bundle"}</strong></div>
              <div><span>目标</span><strong>云端</strong></div>
            </div>
          )}
        </section>
      </div>
    );
  }

  if (detail) {
    const latestBuild = detail.builds[0];
    const selectedRollbackVersion = detail.versions.find(version => (
      version.versionId === selectedRollbackVersionId
      && version.canRollback
      && version.versionId !== detail.currentVersionId
      && version.status.toLowerCase() !== "current"
    ));
    const currentCloudVersion = detail.versions.find(version => (
      version.versionId === detail.currentVersionId || version.status.toLowerCase() === "current"
    ));
    const hasReceipt = detail.deployment.source === "receipt";
    const canUpdate = hasReceipt && detail.deployment.artifactId === "managed-runtime"
      && Boolean(latestBuild)
      && latestBuild.id !== detail.deployment.buildId;
    const chatRoute = resolveCloudChatRoute(detail.deployment);
    return (
      <div className="delivery-page deployment-detail-page" data-layout="document">
        <PageHeaderActions>
          <button className="button secondary" type="button" onClick={closeDetail}>
            <ArrowLeft size={15} /><span>返回云端 Agent</span>
          </button>
          {Boolean(detail.deployment.agentId) && (
            <button
              className="button secondary"
              type="button"
              onClick={() => setRollbackConfirmOpen(true)}
              disabled={!selectedRollbackVersion?.canRollback || updating || rollbackBusy}
            >
              {rollbackBusy ? "回滚中…" : selectedRollbackVersion ? "回滚到所选版本" : "选择版本回滚"}
            </button>
          )}
          {canUpdate && (
            <button className="button secondary" type="button" onClick={() => void updateToBuild(detail.deployment, latestBuild.id)} disabled={updating}>
              {updating ? "更新中…" : "部署最新 Build"}
            </button>
          )}
          {deploymentState(detail.deployment.status) === "ready" && (
            <button
              className="button accent"
              type="button"
              onClick={() => chatRoute.kind === "official-dashboard"
                ? void openHostedUi(detail.deployment)
                : onOpenChat(detail.deployment.id)}
              disabled={updating}
            >
              {chatRoute.kind === "official-dashboard"
                ? <><ExternalLink size={15} />打开官方 Dashboard</>
                : <><MessagesSquare size={15} />进入会话</>}
            </button>
          )}
        </PageHeaderActions>

        <div className="delivery-intro deployment-detail-heading">
          <button className="button tertiary compact" type="button" onClick={closeDetail} aria-label="返回云端 Agent">
            <ArrowLeft size={16} />
          </button>
          <div>
            <h2>{detail.deployment.agentName || detail.sourceAgentName || "云端 Agent"}</h2>
            <p>{detail.deployment.agentId || detail.deployment.id}</p>
          </div>
        </div>

        {detail.error && <div className="form-error" role="alert">{detail.error}</div>}
        <section className="delivery-block" aria-label="云端 Agent 详情">
          <div className="api-contract" aria-label="云端部署事实">
            <div><span>名称</span><strong>{detail.deployment.agentName || detail.sourceAgentName}</strong></div>
            <div><span>来源</span><strong>{hasReceipt ? "Studio 部署记录" : "账号云端 Agent"}</strong></div>
            <div><span>状态</span><strong>{deploymentLabel(detail.deployment.status)}</strong></div>
            <div><span>云端 Agent</span><code>{detail.deployment.agentId || "尚未返回"}</code></div>
            <div><span>类型</span><code>{detail.deployment.framework || detail.deployment.artifactId || "尚未返回"}</code></div>
            <div><span>Endpoint</span><code>{detail.deployment.endpoint || "尚未返回"}</code></div>
            {detail.deployment.instanceId && <div><span>实例</span><code>{detail.deployment.instanceId}</code></div>}
            {hasReceipt && <div><span>当前 Build</span><code>{detail.deployment.buildId}</code></div>}
            <div>
              <span>当前版本</span>
              <code title={currentCloudVersion?.versionId || detail.deployment.versionId || undefined}>
                {currentCloudVersion?.versionName || currentCloudVersion?.tag || detail.deployment.versionId || "尚未返回"}
              </code>
            </div>
            <div><span>更新时间</span><code>{formatUpdatedAt(detail.deployment.updatedAt)}</code></div>
            {hasReceipt && <div><span>Bundle</span><code title={detail.deployment.bundleDigest}>{detail.deployment.bundleDigest}</code></div>}
          </div>
          {detail.deployment.agentId ? <section className="deployment-version-history" aria-label="云端版本历史">
            <div><h3>云端版本历史</h3><p>版本状态与可回滚性来自云端 Server</p></div>
            {detail.loading ? <p>正在读取版本…</p> : (
              <div className="deployment-version-list" role="radiogroup" aria-label="选择回滚版本">
                <div className="deployment-version-header" aria-hidden="true">
                  <span>版本</span><span>状态</span><span>流量</span><span>创建时间</span>
                </div>
                {detail.versions.length ? detail.versions.map(version => {
                  const isCurrent = version.versionId === detail.currentVersionId || version.status.toLowerCase() === "current";
                  const versionState = isCurrent ? "当前" : version.canRollback ? "可回滚" : "不可回滚";
                  return (
                  <button
                    key={version.versionId}
                    type="button"
                    role="radio"
                    aria-checked={version.versionId === selectedRollbackVersionId}
                    aria-label={`${isCurrent ? "当前版本" : version.canRollback ? "可回滚版本" : "不可回滚版本"} ${version.versionName || version.tag || "未命名"}`}
                    className="deployment-version-option"
                    data-current={isCurrent}
                    data-selected={version.versionId === selectedRollbackVersionId}
                    disabled={isCurrent || !version.canRollback || updating || rollbackBusy}
                    onClick={() => setSelectedRollbackVersionId(version.versionId)}
                  >
                    <strong className="deployment-version-name">{version.versionName || version.tag || "未命名版本"}</strong>
                    <span className="deployment-version-state" data-state={isCurrent ? "current" : version.canRollback ? "available" : "disabled"}>{versionState}</span>
                    <span className="deployment-version-traffic">{version.trafficPercentage}%</span>
                    <time className="deployment-version-time" dateTime={version.createdAt || undefined}>{formatUpdatedAt(version.createdAt)}</time>
                  </button>
                  );
                }) : <p className="deployment-version-empty">云端暂未返回版本记录。</p>}
              </div>
            )}
          </section> : <div className="callout"><div><strong>缺少云端 Agent ID</strong><p>当前记录无法查询 Server 版本历史，因此不开放版本回滚。</p></div></div>}
        </section>
        {rollbackConfirmOpen && selectedRollbackVersion && (
          <ConfirmDialog
            title="确认回滚云端 Agent？"
            description={`当前云端版本 ${currentCloudVersion?.versionName || detail.deployment.versionId || "未知"}（${currentCloudVersion?.versionId || detail.deployment.versionId || "未知"}）将回滚到 ${selectedRollbackVersion.versionName || selectedRollbackVersion.tag || "目标版本"}（${selectedRollbackVersion.versionId}）。系统将调用 Server RollbackVersion，并在提交后重新读取 Agent 与版本列表。`}
            confirmText="确认回滚"
            danger={false}
            busy={rollbackBusy}
            onConfirm={() => void submitRollback()}
            onCancel={() => setRollbackConfirmOpen(false)}
          />
        )}
      </div>
    );
  }

  return (
    <div className="delivery-page" data-layout="document">
      <PageHeaderActions>
        <button className="button accent" type="button" onClick={() => navigateToStudioHash("#/deployments/new")}>
          <CloudUpload size={15} /><span>选择 Build 部署</span>
        </button>
        <button className="button secondary" type="button" onClick={() => void refreshAll()} disabled={!deployments.length || refreshing.size > 0}>
          <RefreshCw size={15} /><span>刷新全部状态</span>
        </button>
      </PageHeaderActions>

      <div className="delivery-intro">
        <div><h2>云端 Agent</h2><p>统一管理 Studio 部署和账号下已有的云端 Agent。</p></div>
      </div>

      {error && <div className="form-error" role="alert">{error}</div>}

      <section className="delivery-stat-strip" aria-label="部署事实摘要">
        <div><span className="stat-label">云端 Agent</span><strong>{deployments.length}</strong><small>按 Agent 去重</small></div>
        <div><span className="stat-label">运行中</span><strong>{summary.ready}</strong><small>可访问</small></div>
        <div><span className="stat-label">进行中</span><strong>{summary.pending}</strong><small>创建或更新中</small></div>
        <div><span className="stat-label">异常</span><strong>{summary.failed}</strong><small>需要查看详情</small></div>
      </section>

      {loading ? <div className="delivery-empty-state"><p>正在读取云端 Agent…</p></div> : !deployments.length ? (
        <div className="delivery-empty-state">
          <CloudUpload size={24} /><h2>还没有云端 Agent</h2>
          <p>可以从 Agent 详情构建并部署到云端。</p>
          <div className="delivery-empty-actions">
            <button className="button accent" type="button" onClick={() => navigateToStudioHash("#/deployments/new")}>选择 Build 部署</button>
            <button className="button secondary" type="button" onClick={onCreate}>创建 Agent</button>
          </div>
        </div>
      ) : (
        <section className="delivery-block" aria-label="云端 Agent 列表">
          <h2>Agent 列表</h2><p>同一 Agent 的多次部署聚合为一行；工程事实可在详情中查看。</p>
          <div className="delivery-table-scroll">
            <table className="delivery-table">
              <thead><tr><th>Agent</th><th>状态</th><th>类型</th><th>版本</th><th>更新时间</th><th><span className="sr-only">操作</span></th></tr></thead>
              <tbody>{deployments.map(deployment => {
                const refreshingThis = refreshing.has(deployment.id);
                const chatRoute = resolveCloudChatRoute(deployment);
                return <tr key={deployment.id}>
                  <td>
                    <button
                      className="delivery-agent-identity"
                      type="button"
                      aria-label={`查看 ${deployment.agentName || deployment.agentId || "云端 Agent"} 详情`}
                      onClick={() => void openDetail(deployment)}
                    >
                      <strong>{deployment.agentName || deployment.agentId || "云端 Agent"}</strong>
                      <code title={deployment.agentId || deployment.id}>{shortId(deployment.agentId || deployment.id, 24)}</code>
                    </button>
                  </td>
                  <td><span className="delivery-status-badge" data-state={deploymentState(deployment.status)}>{deploymentLabel(deployment.status)}</span></td>
                  <td><strong>{deployment.framework || (deployment.artifactId === "managed-runtime" ? "YAML Agent" : "高代码 Agent")}</strong><small>{deployment.source === "receipt" ? "Studio 部署记录" : "账号云端 Agent"}</small></td>
                  <td><code title={deployment.versionId || ""}>{shortId(deployment.versionId || "—", 20)}</code></td>
                  <td><span className="delivery-updated-at">{formatUpdatedAt(deployment.updatedAt)}</span></td>
                  <td className="delivery-row-actions">
                    {deploymentState(deployment.status) === "ready" && deployment.agentId && (
                      <button
                        className="button secondary compact"
                        type="button"
                        aria-label={chatRoute.kind === "official-dashboard"
                          ? "打开官方 Dashboard"
                          : "打开云端 Agent 会话"}
                        onClick={() => chatRoute.kind === "official-dashboard"
                          ? void openHostedUi(deployment)
                          : onOpenChat(deployment.id)}
                      >
                        {chatRoute.kind === "official-dashboard"
                          ? <><ExternalLink size={15} /><span>Dashboard</span></>
                          : <><MessagesSquare size={15} /><span>会话</span></>}
                      </button>
                    )}
                    <MoreActionsMenu
                      label={`${deployment.agentName || deployment.agentId || deployment.id} 的更多操作`}
                      items={[
                        { label: refreshingThis ? "正在刷新状态" : "刷新状态", disabled: refreshingThis, onSelect: () => void refresh(deployment) },
                        ...(deploymentState(deployment.status) === "ready" && deployment.agentId
                          ? [{ label: "在 Hosted UI 中打开", onSelect: () => void openHostedUi(deployment) }]
                          : []),
                        ...(deployment.source === "receipt"
                          ? [{ label: "版本管理", onSelect: () => void openDetail(deployment) }]
                          : []),
                        ...(deployment.agentId
                          ? [{ label: "删除云端 Agent", danger: true, onSelect: () => setDeleteTarget(deployment) }]
                          : []),
                      ]}
                    />
                  </td>
                </tr>;
              })}</tbody>
            </table>
          </div>
        </section>
      )}

      {deleteTarget && (
        <ConfirmDialog
          title="删除云端 Agent？"
          description={`将删除 ${deleteTarget.agentId || deleteTarget.id} 的云端实例和本地部署记录。此操作不会删除本地 Agent 与 Build。`}
          confirmText="删除云端 Agent"
          busy={deleting}
          onConfirm={() => void deleteCloudAgent()}
          onCancel={() => setDeleteTarget(null)}
        />
      )}
    </div>
  );
}

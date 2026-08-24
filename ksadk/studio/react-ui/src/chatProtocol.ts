export interface ChatRun {
  id: string;
  agentId: string;
  sessionId: string;
  status?: string;
  input: string;
  output?: string;
  model?: string;
  collaborationMode?: string;
  goalObjective?: string;
  traceId?: string;
  startedAt?: string;
  completedAt?: string;
  durationMs?: number | null;
  usage?: {
    inputTokens?: number;
    outputTokens?: number;
    totalTokens?: number;
    reasoningOutputTokens?: number;
    reported?: boolean;
  };
  error?: { code?: string; message?: string } | null;
}

export interface ChatSession {
  id: string;
  title: string;
  updatedAt: string;
  running: boolean;
  activeStatus?: string;
  runs: ChatRun[];
}

export interface ResponseStreamEvent {
  type: string;
  [key: string]: unknown;
}

export interface ChatStreamState {
  localId: string;
  responseId: string;
  sessionId: string;
  runId: string;
  reasoning: string;
  output: string;
  status: "streaming" | "paused" | "waiting_input" | "completed" | "failed" | "cancelled";
  error: string;
  activities: RunActivity[];
  surfaces: A2UISurface[];
  usage?: Record<string, number>;
  collaborationMode?: string;
  goalObjective?: string;
  startedAt?: string;
}

export interface A2UIInteraction {
  id: string;
  kind: string;
  status: "pending" | "resolved" | "expired";
  inputSchema: Record<string, unknown>;
}

export interface A2UIComponent {
  id: string;
  component: string;
  [key: string]: unknown;
}

export interface A2UISurface {
  id: string;
  catalogId: string;
  components: Record<string, A2UIComponent>;
  roots: string[];
  dataModel: Record<string, unknown>;
  interaction?: A2UIInteraction;
}

export interface RunEvent {
  id: number;
  type: string;
  data?: Record<string, unknown>;
  createdAt?: string;
}

export interface RunActivity {
  id: string;
  kind: "command" | "tool" | "approval";
  title: string;
  status: "running" | "completed" | "failed" | "waiting";
  detail: string;
  data: Record<string, unknown>;
}

export interface RuntimeTextItem {
  runId: string;
  scopeId: string;
  itemId: string;
  partId: string;
  phase: string;
  kind: "message" | "thinking";
  text: string;
  completed: boolean;
}

export interface RunActivityProjection {
  reasoning: string;
  output: string;
  textItems: RuntimeTextItem[];
  activities: RunActivity[];
}

export interface RunInspectorTimelineItem {
  id: string;
  kind: "run" | "thinking" | "message" | "command" | "tool" | "approval" | "usage";
  title: string;
  summary: string;
  detail: string;
  status: RunActivity["status"];
  createdAt?: string;
  data: Record<string, unknown>;
}

export interface ContextUsageState {
  known: boolean;
  usedTokens: number;
  limitTokens: number;
  percent: number;
}

export interface ContextUsageTooltip {
  title: string;
  value: string;
  detail: string;
}

export function contextUsageState(
  inputTokens: number | undefined,
  contextWindowTokens: number | undefined,
): ContextUsageState {
  const limitTokens = Math.max(0, Number(contextWindowTokens) || 0);
  const known = Number.isFinite(inputTokens) && Number(inputTokens) >= 0 && limitTokens > 0;
  const usedTokens = known ? Math.max(0, Number(inputTokens)) : 0;
  const percent = known
    ? Math.max(0, Math.min(100, Math.round((usedTokens / limitTokens) * 100)))
    : 0;
  return { known, usedTokens, limitTokens, percent };
}

export function contextUsageTooltip(state: ContextUsageState): ContextUsageTooltip {
  return {
    title: "上下文窗口",
    value: state.known ? `${state.percent}% 已用` : "用量未上报",
    detail: state.known
      ? `已用 ${state.usedTokens.toLocaleString("en-US")} tokens，共 ${state.limitTokens.toLocaleString("en-US")}`
      : state.limitTokens > 0
        ? `上限 ${state.limitTokens.toLocaleString("en-US")} tokens`
        : "当前模型未提供上下文上限",
  };
}

export function latestReportedInputTokens(
  runs: Array<Pick<ChatRun, "usage">>,
): number | undefined {
  return [...runs]
    .reverse()
    .find(run => run.usage?.reported === true && Number.isFinite(run.usage.inputTokens))
    ?.usage?.inputTokens;
}

export function latestRunningRun<T extends Pick<ChatRun, "id" | "status" | "startedAt">>(
  runs: T[],
): T | undefined {
  return [...runs]
    .filter(run => run.status === "RUNNING")
    .sort((left, right) => Date.parse(left.startedAt || "1970-01-01") - Date.parse(right.startedAt || "1970-01-01"))
    .at(-1);
}

const ACTIVE_RUN_STATUSES = new Set(["RUNNING", "PAUSED", "WAITING_INPUT"]);

export function latestActiveRun<T extends Pick<ChatRun, "id" | "status" | "startedAt">>(
  runs: T[],
): T | undefined {
  return [...runs]
    .filter(run => ACTIVE_RUN_STATUSES.has(String(run.status || "")))
    .sort((left, right) => Date.parse(left.startedAt || "1970-01-01") - Date.parse(right.startedAt || "1970-01-01"))
    .at(-1);
}

export function persistedRunsForDisplay<T extends Pick<ChatRun, "id">>(
  runs: T[],
  optimisticRunId?: string,
): T[] {
  return optimisticRunId ? runs.filter(run => run.id !== optimisticRunId) : runs;
}

function timestamp(run: ChatRun): number {
  const value = run.completedAt || run.startedAt || "";
  const parsed = Date.parse(value);
  return Number.isFinite(parsed) ? parsed : 0;
}

export function groupRunsBySession(runs: ChatRun[], agentId: string): ChatSession[] {
  const grouped = new Map<string, ChatRun[]>();
  for (const run of runs) {
    if (run.agentId !== agentId || !run.sessionId) continue;
    const current = grouped.get(run.sessionId) || [];
    current.push(run);
    grouped.set(run.sessionId, current);
  }

  return [...grouped.entries()]
    .map(([id, items]) => {
      const ordered = [...items].sort((left, right) => timestamp(left) - timestamp(right));
      const first = ordered[0];
      const last = ordered.at(-1)!;
      const active = [...ordered].reverse().find(run => ACTIVE_RUN_STATUSES.has(String(run.status || "")));
      return {
        id,
        title: first?.input?.trim() || "新会话",
        updatedAt: last.completedAt || last.startedAt || "",
        running: Boolean(active),
        activeStatus: active?.status,
        runs: ordered,
      };
    })
    .sort((left, right) => Date.parse(right.updatedAt || "1970-01-01") - Date.parse(left.updatedAt || "1970-01-01"));
}

export function createChatStreamState(localId: string, sessionId: string): ChatStreamState {
  return {
    localId,
    responseId: localId,
    sessionId,
    runId: "",
    reasoning: "",
    output: "",
    status: "streaming",
    error: "",
    activities: [],
    surfaces: [],
  };
}

function recordOf(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" ? value as Record<string, unknown> : {};
}

export function reduceChatStreamEvent(
  state: ChatStreamState,
  event: ResponseStreamEvent,
): ChatStreamState {
  const type = String(event.type || "");
  if (type === "response.created" || type === "response.in_progress") {
    const response = recordOf(event.response);
    return { ...state, responseId: String(response.id || state.responseId) };
  }
  if (type === "response.reasoning_summary_text.delta") {
    return { ...state, reasoning: state.reasoning + String(event.delta || "") };
  }
  if (type === "response.output_text.delta") {
    return { ...state, output: state.output + String(event.delta || "") };
  }
  if (type === "response.output_item.added" || type === "response.output_item.done") {
    const activity = responseItemActivity(recordOf(event.item), type.endsWith(".done"));
    if (!activity) return state;
    const index = state.activities.findIndex(item => item.id === activity.id);
    const activities = [...state.activities];
    if (index < 0) activities.push(activity);
    else activities[index] = { ...activities[index], ...activity };
    return { ...state, activities };
  }
  if (type.startsWith("a2ui.")) {
    return reduceA2UIEvent(state, event);
  }
  if (type === "response.paused") {
    return {
      ...state,
      runId: String(event.runId || event.run_id || state.runId),
      status: "paused",
    };
  }
  if (type === "response.resumed") {
    return {
      ...state,
      runId: String(event.runId || event.run_id || state.runId),
      status: "streaming",
    };
  }
  if (/^response\.(?:web_search_call|file_search_call|mcp_call)\./.test(type)) {
    const id = String(event.item_id || event.itemId || event.call_id || event.callId || type.split(".")[1]);
    const kind: RunActivity["kind"] = "tool";
    const title = type.includes("web_search") ? "网页搜索"
      : type.includes("file_search") ? "文件搜索"
        : "MCP 调用";
    const status: RunActivity["status"] = type.endsWith(".failed") ? "failed"
      : type.endsWith(".completed") ? "completed"
        : "running";
    const activity: RunActivity = { id: `tool:${id}`, kind, title, status, detail: "", data: recordOf(event) };
    const index = state.activities.findIndex(item => item.id === activity.id);
    const activities = [...state.activities];
    if (index < 0) activities.push(activity);
    else activities[index] = { ...activities[index], ...activity };
    return { ...state, activities };
  }
  if (type === "response.completed") {
    const response = recordOf(event.response);
    const metadata = recordOf(response.metadata);
    return {
      ...state,
      responseId: String(response.id || state.responseId),
      runId: String(metadata.runtime_run_id || metadata.runtimeRunId || state.runId),
      usage: recordOf(response.usage) as Record<string, number>,
      status: "completed",
    };
  }
  if (type === "response.cancelled" || type === "response.canceled") {
    return { ...state, status: "cancelled" };
  }
  if (type === "response.failed" || type === "error") {
    const response = recordOf(event.response);
    const error = recordOf(event.error || response.error);
    return {
      ...state,
      status: "failed",
      error: String(error.message || event.message || "Agent 运行失败"),
    };
  }
  return state;
}

function emptySurface(id: string): A2UISurface {
  return {
    id,
    catalogId: "",
    components: {},
    roots: [],
    dataModel: {},
  };
}

function operationList(event: ResponseStreamEvent): Record<string, unknown>[] {
  const raw = event.a2uiOperations ?? event.a2ui_operations ?? event.operations;
  return Array.isArray(raw) ? raw.map(recordOf) : [];
}

function applyA2UIOperations(
  surfaces: Map<string, A2UISurface>,
  operations: Record<string, unknown>[],
): void {
  for (const operation of operations) {
    const create = recordOf(operation.createSurface);
    if (Object.keys(create).length) {
      const id = String(create.surfaceId || create.surface_id || "");
      if (!id) continue;
      const current = surfaces.get(id) || emptySurface(id);
      surfaces.set(id, { ...current, catalogId: String(create.catalogId || create.catalog_id || current.catalogId) });
      continue;
    }
    const update = recordOf(operation.updateComponents);
    if (Object.keys(update).length) {
      const id = String(update.surfaceId || update.surface_id || "");
      if (!id) continue;
      const current = surfaces.get(id) || emptySurface(id);
      const components = { ...current.components };
      const nextComponents = Array.isArray(update.components) ? update.components.map(recordOf) : [];
      for (const raw of nextComponents) {
        const componentId = String(raw.id || raw.componentId || raw.component_id || "");
        const componentType = String(raw.component || raw.type || "");
        if (componentId && componentType) components[componentId] = { ...raw, id: componentId, component: componentType };
      }
      const referenced = new Set<string>();
      for (const component of Object.values(components)) {
        const children = Array.isArray(component.children) ? component.children : [];
        for (const child of children) if (typeof child === "string") referenced.add(child);
        if (typeof component.child === "string") referenced.add(component.child);
      }
      const roots = Object.keys(components).filter(componentId => !referenced.has(componentId));
      surfaces.set(id, { ...current, components, roots });
      continue;
    }
    const data = recordOf(operation.updateDataModel);
    if (Object.keys(data).length) {
      const id = String(data.surfaceId || data.surface_id || "");
      if (!id) continue;
      const current = surfaces.get(id) || emptySurface(id);
      const value = recordOf(data.value);
      surfaces.set(id, { ...current, dataModel: String(data.path || "/") === "/" ? value : { ...current.dataModel, ...value } });
      continue;
    }
    const remove = recordOf(operation.deleteSurface);
    if (Object.keys(remove).length) {
      const id = String(remove.surfaceId || remove.surface_id || "");
      if (id) surfaces.delete(id);
    }
  }
}

function reduceA2UIEvent(state: ChatStreamState, event: ResponseStreamEvent): ChatStreamState {
  const surfaces = new Map(state.surfaces.map(surface => [surface.id, {
    ...surface,
    components: { ...surface.components },
    dataModel: { ...surface.dataModel },
  }]));
  applyA2UIOperations(surfaces, operationList(event));
  const type = String(event.type || "");
  const surfaceId = String(event.surfaceId || event.surface_id || "");
  if (type === "a2ui.interaction" && surfaceId) {
    const current = surfaces.get(surfaceId) || emptySurface(surfaceId);
    surfaces.set(surfaceId, {
      ...current,
      interaction: {
        id: String(event.interactionId || event.interaction_id || ""),
        kind: String(event.kind || "form"),
        status: "pending",
        inputSchema: recordOf(event.inputSchema || event.input_schema),
      },
    });
  }
  if (type === "a2ui.action" && surfaceId) {
    const current = surfaces.get(surfaceId);
    if (current?.interaction) {
      surfaces.set(surfaceId, {
        ...current,
        interaction: { ...current.interaction, status: "resolved" },
      });
    }
  }
  return {
    ...state,
    runId: String(event.runId || event.run_id || state.runId),
    surfaces: [...surfaces.values()],
    status: type === "a2ui.interaction" ? "waiting_input"
      : type === "a2ui.action" ? "streaming"
        : state.status,
  };
}

export function projectA2UISurfaces(events: RunEvent[]): A2UISurface[] {
  let state = createChatStreamState("persisted", "persisted");
  for (const event of events) {
    if (!event.type.startsWith("a2ui.")) continue;
    state = reduceA2UIEvent(state, { type: event.type, ...(event.data || {}) });
  }
  return state.surfaces;
}

export function createResponseSseParser(onEvent: (event: ResponseStreamEvent) => void) {
  let buffer = "";

  const flush = (final = false) => {
    buffer = buffer.replaceAll("\r\n", "\n");
    let boundary = buffer.indexOf("\n\n");
    while (boundary >= 0) {
      parseBlock(buffer.slice(0, boundary));
      buffer = buffer.slice(boundary + 2);
      boundary = buffer.indexOf("\n\n");
    }
    if (final && buffer.trim()) {
      parseBlock(buffer);
      buffer = "";
    }
  };

  const parseBlock = (block: string) => {
    let eventName = "message";
    const data: string[] = [];
    for (const line of block.split("\n")) {
      if (!line || line.startsWith(":")) continue;
      if (line.startsWith("event:")) eventName = line.slice(6).trim();
      if (line.startsWith("data:")) data.push(line.slice(5).trimStart());
    }
    if (!data.length) return;
    const raw = data.join("\n");
    if (raw === "[DONE]") return;
    try {
      const parsed = JSON.parse(raw) as ResponseStreamEvent;
      onEvent({ ...parsed, type: String(parsed.type || eventName) });
    } catch {
      onEvent({ type: eventName, message: raw });
    }
  };

  return {
    push(chunk: string) {
      buffer += chunk;
      flush();
    },
    finish() {
      flush(true);
    },
  };
}

function callKey(data: Record<string, unknown>, fallback: string): string {
  return String(data.callId || data.call_id || data.toolCallId || data.tool_call_id || fallback);
}

function eventTitle(kind: RunActivity["kind"], data: Record<string, unknown>): string {
  if (kind === "command") return String(data.command || data.name || "执行命令");
  if (kind === "tool") return String(data.name || data.tool || "调用工具");
  return String(data.kind || data.action || "等待批准");
}

function eventDetail(data: Record<string, unknown>): string {
  const selected = data.output ?? data.result ?? data.message ?? data.error ?? "";
  if (typeof selected === "string") return selected;
  if (selected && typeof selected === "object") return JSON.stringify(selected, null, 2);
  return selected === "" ? "" : String(selected);
}

function responseItemActivity(item: Record<string, unknown>, done: boolean): RunActivity | null {
  const itemType = String(item.type || "");
  if (!["function_call", "mcp_call", "shell_call", "local_shell_call", "file_search_call", "web_search_call", "approval_request"].includes(itemType)) {
    return null;
  }
  const id = String(item.call_id || item.callId || item.id || "tool");
  const commandLike = itemType === "shell_call" || itemType === "local_shell_call";
  const approvalLike = itemType === "approval_request";
  const kind: RunActivity["kind"] = approvalLike ? "approval" : commandLike ? "command" : "tool";
  const action = recordOf(item.action);
  const commands = Array.isArray(action.commands)
    ? action.commands.filter(value => typeof value === "string").join(" && ")
    : "";
  const title = approvalLike
    ? String(action.title || action.kind || "等待批准")
    : commandLike
    ? commands || String(item.name || "执行命令")
    : itemType === "web_search_call" ? "网页搜索"
      : itemType === "file_search_call" ? "文件搜索"
        : String(item.name || item.server_label || "调用工具");
  const rawStatus = String(item.status || "");
  const failed = rawStatus === "failed" || rawStatus === "error"
    || Number(item.exit_code ?? item.exitCode ?? 0) !== 0;
  const status: RunActivity["status"] = failed ? "failed" : approvalLike && !done ? "waiting" : done ? "completed" : "running";
  return {
    id: `${kind}:${id}`,
    kind,
    title,
    status,
    detail: eventDetail(item),
    data: item,
  };
}

function textItemKey(data: Record<string, unknown>): string {
  return [
    String(data.runId || ""),
    String(data.scopeId || ""),
    String(data.itemId || ""),
    String(data.partId || ""),
  ].join("/");
}

function outputRefsFrom(event: RunEvent): Array<Record<string, unknown>> {
  const runtimeEvent = recordOf(event.data?.runtimeEvent);
  const raw = runtimeEvent.output_refs ?? runtimeEvent.outputRefs;
  return Array.isArray(raw) ? raw.map(recordOf) : [];
}

export function projectRunActivities(events: RunEvent[]): RunActivityProjection {
  const textItems: RuntimeTextItem[] = [];
  const textByKey = new Map<string, number>();
  const activities: RunActivity[] = [];
  const byKey = new Map<string, number>();
  let terminalOutputRefs: Array<Record<string, unknown>> | null = null;

  for (const event of events) {
    const data = event.data || {};
    const isThinking = event.type === "thinking.delta" || event.type === "thinking.completed";
    const isMessage = event.type === "message.delta" || event.type === "message.completed";
    if (isThinking || isMessage) {
      const kind: RuntimeTextItem["kind"] = isThinking ? "thinking" : "message";
      const completed = event.type.endsWith(".completed");
      const operation = completed ? "complete" : String(data.operation || "append");
      const text = String(data.text || data.delta || "");
      const key = `${kind}:${textItemKey(data)}`;
      const existingIndex = textByKey.get(key);
      const runtimeEvent = recordOf(data.runtimeEvent);
      const phase = String(data.phase || runtimeEvent.phase || "");
      if (existingIndex === undefined) {
        textByKey.set(key, textItems.length);
        textItems.push({
          runId: String(data.runId || ""),
          scopeId: String(data.scopeId || ""),
          itemId: String(data.itemId || ""),
          partId: String(data.partId || ""),
          phase,
          kind,
          text,
          completed,
        });
      } else {
        const previous = textItems[existingIndex];
        const nextText = operation === "append" ? previous.text + text : text || previous.text;
        textItems[existingIndex] = {
          ...previous,
          phase: previous.phase || phase,
          text: nextText,
          completed: completed || previous.completed,
        };
      }
      continue;
    }

    if (["run.completed", "run.failed", "run.interrupted", "run.cancelled", "run.canceled"].includes(event.type)) {
      const refs = outputRefsFrom(event);
      if (refs.length) terminalOutputRefs = refs;
      continue;
    }

    let kind: RunActivity["kind"] | null = null;
    if (event.type.startsWith("command.")) kind = "command";
    else if (event.type.startsWith("tool.")) kind = "tool";
    else if (event.type === "approval.requested") kind = "approval";
    if (!kind) continue;

    const key = callKey(data, `${kind}-${event.id}`);
    const existingIndex = byKey.get(`${kind}:${key}`);
    const completed = event.type.endsWith(".completed");
    const failed = event.type.endsWith(".failed") || Boolean(data.error)
      || Number(data.exitCode ?? data.exit_code ?? 0) !== 0;
    const status: RunActivity["status"] = kind === "approval"
      ? "waiting"
      : failed
        ? "failed"
        : completed
          ? "completed"
          : "running";
    const next: RunActivity = {
      id: `${kind}:${key}`,
      kind,
      title: eventTitle(kind, data),
      status,
      detail: eventDetail(data),
      data,
    };
    if (existingIndex === undefined) {
      byKey.set(next.id, activities.length);
      activities.push(next);
    } else {
      const previous = activities[existingIndex];
      activities[existingIndex] = {
        ...previous,
        ...next,
        title: next.title === eventTitle(kind, {}) ? previous.title : next.title,
        detail: next.detail || previous.detail,
      };
    }
  }

  const messageItems = textItems.filter(item => item.kind === "message");
  const thinkingItems = textItems.filter(item => item.kind === "thinking");
  const reasoning = thinkingItems.map(item => item.text).join("");

  let output: string;
  if (terminalOutputRefs && terminalOutputRefs.length) {
    const byIdentity = new Map<string, RuntimeTextItem>();
    for (const item of messageItems) byIdentity.set(`${item.scopeId}/${item.itemId}`, item);
    output = terminalOutputRefs
      .map(ref => byIdentity.get(`${String(ref.scope_id ?? ref.scopeId ?? "")}/${String(ref.item_id ?? ref.itemId ?? "")}`))
      .filter((item): item is RuntimeTextItem => Boolean(item))
      .map(item => item.text)
      .join("\n\n");
  } else {
    const completed = messageItems.filter(item => item.completed);
    output = (completed.length ? completed : messageItems).map(item => item.text).join("");
  }

  return { reasoning, output, textItems, activities };
}

function tokenSummary(data: Record<string, unknown>): string {
  const total = Number(data.totalTokens ?? data.total_tokens ?? 0);
  return `${Number.isFinite(total) ? total.toLocaleString("en-US") : "0"} tokens`;
}

function durationSummary(data: Record<string, unknown>): string {
  const duration = Number(data.durationMs ?? data.duration_ms);
  if (!Number.isFinite(duration) || duration < 0) return "";
  return duration < 1000 ? `${Math.round(duration)}ms` : `${(duration / 1000).toFixed(1)}s`;
}

/** Turn the persisted event stream into a compact, stable run-inspector timeline. */
export function projectRunInspectorTimeline(events: RunEvent[]): RunInspectorTimelineItem[] {
  const timeline: RunInspectorTimelineItem[] = [];
  const indexes = new Map<string, number>();
  const hasStarted = events.some(event => event.type === "run.started");

  const upsert = (key: string, item: RunInspectorTimelineItem) => {
    const index = indexes.get(key);
    if (index === undefined) {
      indexes.set(key, timeline.length);
      timeline.push(item);
      return;
    }
    timeline[index] = { ...timeline[index], ...item };
  };

  for (const event of events) {
    const data = event.data || {};
    const type = event.type || "";

    if (type === "run.created") {
      if (hasStarted) continue;
      timeline.push({
        id: `run:${event.id}`,
        kind: "run",
        title: "Run 创建",
        summary: String(data.model || data.runtimeType || ""),
        detail: "",
        status: "running",
        createdAt: event.createdAt,
        data,
      });
      continue;
    }
    if (type === "run.started") {
      const runtimeEvent = recordOf(data.runtimeEvent);
      timeline.push({
        id: `run:${event.id}`,
        kind: "run",
        title: "Run 启动",
        summary: String(data.runtimeType || runtimeEvent.runtimeType || runtimeEvent.model || "Local Runtime"),
        detail: "",
        status: "running",
        createdAt: event.createdAt,
        data,
      });
      continue;
    }
    if (["run.completed", "run.failed", "run.interrupted", "run.cancelled", "run.canceled"].includes(type)) {
      const failed = type === "run.failed" || type === "run.interrupted";
      const cancelled = type === "run.cancelled" || type === "run.canceled";
      timeline.push({
        id: `run:${event.id}`,
        kind: "run",
        title: failed ? (type === "run.interrupted" ? "Run 中断" : "Run 失败") : cancelled ? "Run 取消" : "Run 完成",
        summary: durationSummary(data),
        detail: failed ? String(data.error || data.message || "") : "",
        status: failed ? "failed" : "completed",
        createdAt: event.createdAt,
        data,
      });
      continue;
    }

    if (type.startsWith("thinking.") || type.startsWith("message.")) {
      const kind = type.startsWith("thinking.") ? "thinking" : "message";
      const key = `stream:${kind}`;
      const previousIndex = indexes.get(key);
      const previous = previousIndex === undefined ? null : timeline[previousIndex];
      const text = String(data.text || data.delta || "");
      const completed = type.endsWith(".completed");
      const detail = completed && text ? text : `${previous?.detail || ""}${text}`;
      upsert(key, {
        id: key,
        kind,
        title: kind === "thinking" ? "思考过程" : "模型回复",
        summary: detail ? `${detail.length} 字` : "",
        detail,
        status: completed ? "completed" : "running",
        createdAt: event.createdAt || previous?.createdAt,
        data,
      });
      continue;
    }

    let activityKind: RunInspectorTimelineItem["kind"] | null = null;
    if (type.startsWith("command.")) activityKind = "command";
    else if (type.startsWith("tool.")) activityKind = "tool";
    else if (type.startsWith("approval.")) activityKind = "approval";
    if (activityKind) {
      const key = `${activityKind}:${callKey(data, String(event.id))}`;
      const previousIndex = indexes.get(key);
      const previous = previousIndex === undefined ? null : timeline[previousIndex];
      const failed = type.endsWith(".failed") || Boolean(data.error)
        || Number(data.exitCode ?? data.exit_code ?? 0) !== 0;
      const completed = type.endsWith(".completed") || type.endsWith(".resolved");
      const detail = eventDetail(data) || previous?.detail || "";
      const nextTitle = eventTitle(activityKind, data);
      const defaultTitle = eventTitle(activityKind, {});
      upsert(key, {
        id: key,
        kind: activityKind,
        title: nextTitle === defaultTitle && previous?.title ? previous.title : nextTitle,
        summary: durationSummary(data),
        detail,
        status: failed ? "failed" : completed ? "completed" : activityKind === "approval" ? "waiting" : "running",
        createdAt: event.createdAt || previous?.createdAt,
        data,
      });
      continue;
    }

    if (type === "usage.reported") {
      timeline.push({
        id: `usage:${event.id}`,
        kind: "usage",
        title: "用量上报",
        summary: tokenSummary(data),
        detail: "",
        status: "completed",
        createdAt: event.createdAt,
        data,
      });
    }
  }

  const terminal = [...events].reverse().find(event => [
    "run.completed", "run.failed", "run.interrupted", "run.cancelled", "run.canceled",
  ].includes(event.type));
  if (terminal) {
    for (const item of timeline) {
      if (item.status === "running" && (item.kind === "thinking" || item.kind === "message")) {
        item.status = "completed";
      }
    }
  }
  return timeline;
}

// ---------------------------------------------------------------------------
// agent-kernel/v1 contracts (digest 69771d8d…)
//
// Hand-written from contracts/agent-kernel/v1/*.schema.json and kept in sync
// with @kingsoftcloud/ksadk-web's decoder (src/types/agent-control.ts).
// Studio cannot depend on the npm package yet, so only the contract types and
// strict decoders live here; stream reducers stay in chatProtocol.ts and are
// never duplicated from ksadk-web.
// ---------------------------------------------------------------------------

export class ContractMismatchError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "ContractMismatchError";
  }
}

export type AgentControlCommandType =
  | "enqueue" | "steer" | "inject" | "interrupt"
  | "pause" | "resume" | "submit_interaction";

export type AgentControlReceiptStatus =
  | "accepted" | "duplicate" | "rejected" | "unsupported"
  | "queue_full" | "persistence_uncertain";

export interface AgentControlError {
  code: string;
  message: string;
  retryable: boolean;
  details?: Record<string, unknown>;
}

export interface AgentControlReceipt {
  schema_version: 1;
  command_id: string;
  status: AgentControlReceiptStatus;
  message_id?: string | null;
  run_id?: string | null;
  accepted_seq?: number | null;
  error?: AgentControlError | null;
  /** Unknown optional fields kept verbatim for forward compatibility. */
  extensions: Record<string, unknown>;
}

const RECEIPT_STATUSES: ReadonlySet<string> = new Set([
  "accepted", "duplicate", "rejected", "unsupported",
  "queue_full", "persistence_uncertain",
]);

const RECEIPT_KNOWN_KEYS: ReadonlySet<string> = new Set([
  "schema_version", "command_id", "status", "message_id", "run_id",
  "accepted_seq", "error",
]);

function asRecord(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null;
}

function decodeControlError(raw: unknown): AgentControlError {
  const value = asRecord(raw);
  if (
    !value
    || typeof value.code !== "string" || !value.code
    || typeof value.message !== "string"
    || typeof value.retryable !== "boolean"
  ) {
    throw new ContractMismatchError("AgentControlReceipt/v1 mismatch: malformed control error");
  }
  const details = asRecord(value.details) || undefined;
  return {
    code: value.code,
    message: value.message,
    retryable: value.retryable,
    ...(details ? { details } : {}),
  };
}

/** Strict decoder for AgentControlReceipt/v1; unknown optional fields go to extensions. */
export function decodeReceipt(raw: unknown): AgentControlReceipt {
  const value = asRecord(raw);
  if (!value) {
    throw new ContractMismatchError("AgentControlReceipt/v1 mismatch: not an object");
  }
  if (value.schema_version !== 1) {
    throw new ContractMismatchError("AgentControlReceipt/v1 mismatch: schema_version must be 1");
  }
  if (typeof value.command_id !== "string" || !value.command_id) {
    throw new ContractMismatchError("AgentControlReceipt/v1 mismatch: command_id required");
  }
  if (typeof value.status !== "string" || !RECEIPT_STATUSES.has(value.status)) {
    throw new ContractMismatchError("AgentControlReceipt/v1 mismatch: unknown status");
  }
  const status = value.status as AgentControlReceiptStatus;
  if (
    (status === "rejected" || status === "unsupported"
      || status === "queue_full" || status === "persistence_uncertain")
    && value.error == null
  ) {
    throw new ContractMismatchError(
      `AgentControlReceipt/v1 mismatch: status ${status} requires error`,
    );
  }
  const extensions: Record<string, unknown> = {};
  for (const [key, entry] of Object.entries(value)) {
    if (!RECEIPT_KNOWN_KEYS.has(key)) {
      extensions[key] = entry;
    }
  }
  return {
    schema_version: 1,
    command_id: value.command_id,
    status,
    message_id: (value.message_id as string | null) ?? null,
    run_id: (value.run_id as string | null) ?? null,
    accepted_seq: (value.accepted_seq as number | null) ?? null,
    error: value.error == null ? null : decodeControlError(value.error),
    extensions,
  };
}

export interface RuntimeCapability {
  supported: boolean;
  mode: "native" | "emulated" | "unavailable";
  reason?: string | null;
}

export interface RuntimeCapabilityMatrix {
  schema_version: 1;
  cancel: RuntimeCapability;
  pause: RuntimeCapability;
  resume: RuntimeCapability;
  submit_interaction: RuntimeCapability;
  attach: RuntimeCapability;
  steer: RuntimeCapability;
  inject: RuntimeCapability;
  checkpoint: RuntimeCapability;
  durable_restore: RuntimeCapability;
  goal?: RuntimeCapability | null;
  loop?: RuntimeCapability | null;
  plan?: RuntimeCapability | null;
}

const CAPABILITY_KEYS = [
  "cancel", "pause", "resume", "submit_interaction", "attach",
  "steer", "inject", "checkpoint", "durable_restore",
] as const;

const EXECUTION_MODE_KEYS = ["goal", "loop", "plan"] as const;

function decodeRuntimeCapability(raw: unknown, path: string): RuntimeCapability {
  const capability = asRecord(raw);
  if (
    !capability
    || typeof capability.supported !== "boolean"
    || (capability.mode !== "native" && capability.mode !== "emulated" && capability.mode !== "unavailable")
  ) {
    throw new ContractMismatchError(`RuntimeCapabilityMatrix/v1 mismatch at ${path}`);
  }
  if (!capability.supported && (capability.mode !== "unavailable" || typeof capability.reason !== "string" || !capability.reason)) {
    throw new ContractMismatchError(
      `RuntimeCapabilityMatrix/v1 mismatch at ${path}: unsupported capability requires mode=unavailable and reason`,
    );
  }
  return {
    supported: capability.supported,
    mode: capability.mode,
    reason: (capability.reason as string | null) ?? null,
  };
}

/** Strict decoder for RuntimeCapabilityMatrix/v1. */
export function decodeCapabilityMatrix(raw: unknown): RuntimeCapabilityMatrix {
  const value = asRecord(raw);
  if (!value || value.schema_version !== 1) {
    throw new ContractMismatchError("RuntimeCapabilityMatrix/v1 mismatch: schema_version must be 1");
  }
  const matrix = { schema_version: 1 as const } as RuntimeCapabilityMatrix;
  for (const key of CAPABILITY_KEYS) {
    matrix[key] = decodeRuntimeCapability(value[key], key);
  }
  for (const key of EXECUTION_MODE_KEYS) {
    if (value[key] !== undefined && value[key] !== null) {
      matrix[key] = decodeRuntimeCapability(value[key], key);
    }
  }
  return matrix;
}

export interface SessionEventEnvelope {
  schema_version: 1;
  event_id: string;
  session_id: string;
  seq: number;
  timestamp: string;
  family: string;
  family_version: number;
  event_type: string;
  payload: Record<string, unknown>;
  run_id?: string | null;
  extensions: Record<string, unknown>;
}

const ENVELOPE_KNOWN_KEYS: ReadonlySet<string> = new Set([
  "schema_version", "event_id", "session_id", "seq", "timestamp",
  "family", "family_version", "event_type", "payload", "run_id",
  "causation_id", "correlation_id", "actor_ref",
]);

const KNOWN_FAMILIES: ReadonlyMap<string, number> = new Map([
  ["control", 1],
  ["runtime", 2],
]);

/**
 * Decode a SessionEventEnvelope/v1. Unknown families decode successfully so
 * the session cursor can still advance; structural violations fail.
 */
export function decodeSessionEventEnvelope(
  raw: unknown,
): { ok: true; value: SessionEventEnvelope } | { ok: false; error: ContractMismatchError } {
  const value = asRecord(raw);
  const fail = (message: string) => ({
    ok: false as const,
    error: new ContractMismatchError(message),
  });
  if (!value || value.schema_version !== 1) {
    return fail("SessionEventEnvelope/v1 mismatch: schema_version must be 1");
  }
  if (
    typeof value.event_id !== "string" || !value.event_id
    || typeof value.session_id !== "string" || !value.session_id
    || typeof value.timestamp !== "string" || !value.timestamp
    || typeof value.event_type !== "string" || !value.event_type
    || !asRecord(value.payload)
  ) {
    return fail("SessionEventEnvelope/v1 mismatch: required field missing");
  }
  if (typeof value.seq !== "number" || !Number.isInteger(value.seq) || value.seq < 0) {
    return fail("SessionEventEnvelope/v1 mismatch: seq must be a non-negative integer");
  }
  if (typeof value.family !== "string" || !value.family
    || typeof value.family_version !== "number"
    || !Number.isInteger(value.family_version) || value.family_version < 1
  ) {
    return fail("SessionEventEnvelope/v1 mismatch: family/family_version invalid");
  }
  const expectedVersion = KNOWN_FAMILIES.get(value.family);
  if (expectedVersion !== undefined && value.family_version !== expectedVersion) {
    return fail(`SessionEventEnvelope/v1 mismatch: family ${value.family} requires family_version ${expectedVersion}`);
  }
  const extensions: Record<string, unknown> = {};
  for (const [key, entry] of Object.entries(value)) {
    if (!ENVELOPE_KNOWN_KEYS.has(key)) {
      extensions[key] = entry;
    }
  }
  return {
    ok: true,
    value: {
      schema_version: 1,
      event_id: value.event_id,
      session_id: value.session_id,
      seq: value.seq,
      timestamp: value.timestamp,
      family: value.family,
      family_version: value.family_version,
      event_type: value.event_type,
      payload: asRecord(value.payload)!,
      run_id: (value.run_id as string | null) ?? null,
      extensions,
    },
  };
}

/** Two events sharing a seq but differing in content are a protocol error. */
export class SessionEventConflictError extends Error {
  constructor(public readonly seq: number) {
    super(`Session event seq ${seq} was received twice with conflicting content`);
    this.name = "SessionEventConflictError";
  }
}

const DISPLAYABLE_FAMILIES: ReadonlySet<string> = new Set(["runtime"]);

/**
 * Unified session cursor: dedupes/orders strictly by the Session seq.
 * Responses/AG-UI/A2A internal event ids are never used as a reconnect
 * cursor; unknown families advance the cursor but are not displayed.
 */
export class SessionEventCursor {
  private eventsBySeq = new Map<number, SessionEventEnvelope>();
  private lastSeqValue = 0;

  accept(raw: unknown): void {
    const decoded = decodeSessionEventEnvelope(raw);
    if (!decoded.ok) throw decoded.error;
    const incoming = decoded.value;
    const existing = this.eventsBySeq.get(incoming.seq);
    if (existing) {
      if (JSON.stringify(existing) !== JSON.stringify(incoming)) {
        throw new SessionEventConflictError(incoming.seq);
      }
      return;
    }
    this.eventsBySeq.set(incoming.seq, incoming);
    this.lastSeqValue = Math.max(this.lastSeqValue, incoming.seq);
  }

  get lastSeq(): number {
    return this.lastSeqValue;
  }

  reconnectAfterSeq(): number {
    return this.lastSeqValue;
  }

  displayableEvents(): SessionEventEnvelope[] {
    return [...this.eventsBySeq.values()]
      .filter(event => DISPLAYABLE_FAMILIES.has(event.family))
      .sort((left, right) => left.seq - right.seq);
  }
}

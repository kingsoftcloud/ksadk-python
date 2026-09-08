import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  ArrowLeft, ArrowRight, Zap, MessagesSquare, Upload, Folder, Check,
  PanelRight, Plus, Search, Cpu, Wrench, Network, Sparkles, Package, Bot,
  RefreshCw, Send, CircleAlert, ShieldCheck,
} from "lucide-react";
import { zodResolver } from "@hookform/resolvers/zod";
import { FormProvider, useForm, type Resolver } from "react-hook-form";
import { apiFetch } from "../api";
import { AgentEditor } from "./AgentEditor";
import { McpConnectDrawer, ModelCredentialDrawer } from "./ResourcesPage";
import type { StudioViewportMode } from "../responsiveViewport";
import { generateAgentSlug } from "../lib/generatedId";
import { GeneratedIdField } from "../components/ui/GeneratedIdField";
import { StudioMultiSelect } from "../components/ui/StudioMultiSelect";
import { StudioSelect } from "../components/ui/StudioSelect";
import { FileDropzone } from "../components/ui/FileDropzone";
import { TextShimmer } from "../components/ui/TextShimmer";
import { FormField } from "../components/ui/FormField";
import { StudioDrawer } from "../components/ui/StudioDialog";
import { CodeViewer } from "../components/ui/CodeViewer";
import { PageHeaderActions } from "../components/PageHeaderPortal";
import { applyApiFieldErrors } from "../lib/formErrors";
import { mcpUnavailableReason } from "../lib/mcpCompatibility";
import {
  parseProviderConfig,
  providerOptionDescription,
  type AgentProviderCatalogItem,
} from "../agentProviders";
import {
  agentImportSchema,
  conversationCommitSchema,
  projectImportSchema,
  quickAgentSchema,
  type AgentImportFormValues,
  type ConversationCommitFormValues,
  type ProjectImportFormValues,
  type QuickAgentFormValues,
} from "../schemas/agentForms";

/* 四种创建方式；quick 模式即四步向导。 */
type Mode = "quick" | "conversation" | "import" | "project";
type Template = "blank" | "research";

interface ResItem {
  resourceId: string; kind: string; name: string; displayName: string;
  version: string; status: string; source: string; contract?: any;
  requiredSecretRefs?: string[];
  description?: string;
  health?: { toolCount?: number };
}

const DRAFT_PREFIX = "agentkit.studio.agentDraft.v1";
const CODEX_AGENT_PROVIDER_PREFIX = "plugin://io.ksadk.codex-provider@";
const BUILTIN_RUNTIME_OPTIONS = [
  { value: "codex", label: "Codex · ManagedRuntime" },
  { value: "adk", label: "Google ADK · Python source" },
  { value: "langgraph", label: "LangGraph · Python graph" },
];

function isCodexAgentProvider(providerRef: string): boolean {
  return providerRef.startsWith(CODEX_AGENT_PROVIDER_PREFIX);
}

function credentialReference(item?: ResItem): string {
  return item?.requiredSecretRefs?.[0]
    || item?.contract?.credentialRef
    || item?.contract?.credential_ref
    || item?.contract?.requiredSecretRefs?.[0]
    || item?.contract?.required_secret_refs?.[0]
    || "";
}

const POLICY_META: Record<string, { title: string; description: string }> = {
  loose: { title: "宽松权限策略", description: "本地 Tool 全部自动允许，外部操作仍需审批。" },
  strict: { title: "严格权限策略", description: "只读 Tool 自动允许，外部或写入操作需要审批。" },
  custom: { title: "自定义权限策略", description: "沿用每个 Tool Contract 中配置的审批策略。" },
};

const RUNTIME_OPTIONS = [
  { value: "codex", label: "Codex · ManagedRuntime" },
  { value: "adk", label: "Google ADK · Python source" },
  { value: "langgraph", label: "LangGraph · Python graph" },
];
const WIZARD_STEP_META = [
  ["定义 Agent", "模板与系统提示词"],
  ["绑定能力", "Model · Tool · MCP · Skill"],
  ["Prompt 与策略", "检查并调整"],
  ["检查并创建", "构建与打开会话"],
];

const TERMINAL_BUILD_OPERATION_STATES = new Set(["SUCCEEDED", "FAILED", "CANCELLED", "TIMED_OUT"]);
const PROXY_MODEL_FAMILIES = ["deepseek", "glm", "kimi", "minimax", "qwen"];

function modelSortKey(item: ResItem): [number, number[], string] {
  const raw = String(item.contract?.model || item.name || "").toLowerCase();
  const normalized = raw.includes("/") ? raw.split("/", 2)[1] : raw;
  const normalizedForFamily = normalized.replaceAll(".", "-");
  const familyIndex = PROXY_MODEL_FAMILIES.findIndex(family => (
    normalizedForFamily === family
    || normalizedForFamily.startsWith(family === "qwen" ? family : `${family}-`)
  ));
  const numbers = (normalized.match(/\d+/g) || []).slice(0, 8).map(value => -Number(value));
  while (numbers.length < 8) numbers.push(0);
  return [familyIndex < 0 ? PROXY_MODEL_FAMILIES.length : familyIndex, numbers, normalized];
}

function compareStudioModels(left: ResItem, right: ResItem): number {
  const [leftFamily, leftVersion, leftName] = modelSortKey(left);
  const [rightFamily, rightVersion, rightName] = modelSortKey(right);
  if (leftFamily !== rightFamily) return leftFamily - rightFamily;
  for (let index = 0; index < leftVersion.length; index += 1) {
    if (leftVersion[index] !== rightVersion[index]) return leftVersion[index] - rightVersion[index];
  }
  return leftName.localeCompare(rightName);
}

/** The API keeps the complete structured proposal in the conversation context.
 *  The transcript deliberately renders a small, human-readable acknowledgement
 *  instead of leaking a JSON patch into every assistant turn. */
function summarizeConversationProposal(content: string): { title: string; body: string } {
  try {
    const proposal = JSON.parse(content);
    const instructions = proposal?.spec?.instructions || proposal?.instructions || {};
    const name = String(proposal?.name || "未命名 Agent").trim();
    const body = String(
      proposal?.description
      || proposal?.spec?.description
      || instructions?.task
      || instructions?.system
      || "已根据这轮对话更新草稿。",
    ).trim();
    return { title: `已更新草稿：${name}`, body };
  } catch {
    return { title: "已更新草稿", body: content || "已根据这轮对话更新草稿。" };
  }
}

/** Keep only the semantic patch in multi-turn browser context.  Runtime model
 *  profiles and capabilities are sent as separately validated request fields,
 *  so repeating their catalog contracts here only bloats the next LLM turn. */
function compactConversationProposal(proposal: any): any {
  const instructions = proposal?.spec?.instructions || proposal?.instructions || {};
  return {
    name: proposal?.name || "",
    slug: proposal?.slug || "",
    runtimeType: proposal?.runtimeType || "",
    description: proposal?.description || proposal?.spec?.description || "",
    spec: {
      instructions: {
        system: instructions?.system || "",
        task: instructions?.task || "",
      },
    },
  };
}

function mergeAgentSpec(base: any, patch: any): any {
  if (!base || typeof base !== "object" || Array.isArray(base)) return patch;
  if (!patch || typeof patch !== "object" || Array.isArray(patch)) return patch;
  const merged = { ...base };
  for (const [key, value] of Object.entries(patch)) {
    merged[key] = (
      value && typeof value === "object" && !Array.isArray(value)
      && merged[key] && typeof merged[key] === "object" && !Array.isArray(merged[key])
    ) ? mergeAgentSpec(merged[key], value) : value;
  }
  return merged;
}

async function waitForCreatedBuild(operationId: string) {
  for (let attempt = 0; attempt < 1200; attempt += 1) {
    const response = await apiFetch(`/api/v1/operations/${encodeURIComponent(operationId)}`);
    const operation = await response.json().catch(() => null);
    if (!response.ok) {
      throw new Error(operation?.error?.message || `构建状态获取失败（${response.status}）`);
    }
    if (TERMINAL_BUILD_OPERATION_STATES.has(operation?.status)) {
      if (operation.status !== "SUCCEEDED") {
        throw new Error(operation.error?.message || "构建未完成");
      }
      return operation;
    }
    await new Promise(resolve => window.setTimeout(resolve, 200));
  }
  throw new Error("构建等待超时");
}

export function CreatePage({ editingAgentId, viewportMode, onCreated, onAgentsChanged }: {
  editingAgentId?: string;
  viewportMode: StudioViewportMode;
  onBack: () => void;
  onCreated: (id?: string, openChat?: boolean) => void;
  onAgentsChanged?: () => void;
}) {
  const [mode, setMode] = useState<Mode>("quick");
  const [createRailOpen, setCreateRailOpen] = useState(false);
  const [draftState, setDraftState] = useState("尚未保存");
  const [catalog, setCatalog] = useState<ResItem[]>([]);
  const [agentProviders, setAgentProviders] = useState<AgentProviderCatalogItem[]>([]);
  const [selectedProviderRef, setSelectedProviderRef] = useState("");
  const [providerConfigText, setProviderConfigText] = useState("{}");
  const [providerPermissionsApproved, setProviderPermissionsApproved] = useState(false);
  const [credentialStatuses, setCredentialStatuses] = useState<Record<string, { configured?: boolean }>>({});
  /* 向导状态 */
  const [step, setStep] = useState(1);
  const [maxStep, setMaxStep] = useState(1);
  const quickForm = useForm<QuickAgentFormValues>({
    resolver: zodResolver(quickAgentSchema) as Resolver<QuickAgentFormValues>,
    defaultValues: {
      name: "New Agent",
      slug: generateAgentSlug(),
      runtimeType: "codex",
      template: "blank",
      prompt: "",
      description: "",
      audience: "产品与技术负责人",
      language: "zh-CN",
      depth: "deep",
      format: "report",
      systemPrompt: "",
      taskPrompt: "",
      buildAfterCreate: true,
    },
  });
  const {
    name,
    slug,
    runtimeType: runtime,
    template,
    description,
    prompt,
    audience,
    language,
    depth,
    format,
    systemPrompt,
    taskPrompt,
    buildAfterCreate,
  } = quickForm.watch();
  const [selectedModels, setSelectedModels] = useState<string[]>([]);
  const [selectedTools, setSelectedTools] = useState<string[]>([]);
  const [selectedMcp, setSelectedMcp] = useState<string[]>([]);
  const [selectedSkills, setSelectedSkills] = useState<string[]>([]);
  const [policy, setPolicy] = useState("strict");
  const [contextOwnership, setContextOwnership] = useState("auto");
  const [contextEngineRollout, setContextEngineRollout] = useState("shadow");
  const [memoryEnabled, setMemoryEnabled] = useState(false);
  const [memoryWriteRollout, setMemoryWriteRollout] = useState("shadow");
  const contextOwnershipOptions = useMemo(() => {
    const automatic = {
      value: "auto",
      label: "自动（推荐）",
      description: "根据 Runtime 能力选择安全模式",
    };
    if (runtime === "codex") {
      return [
        automatic,
        {
          value: "native",
          label: "原生 Runtime 管理",
          description: "由 Codex 等原生 Runtime 管理最终上下文",
        },
      ];
    }
    if (runtime === "langgraph") {
      return [
        automatic,
        { value: "framework", label: "框架管理", description: "保留 LangGraph 原有行为" },
        { value: "ksadk", label: "KsADK 管理", description: "统一编译 Prompt 并规划上下文" },
      ];
    }
    return [
      automatic,
      { value: "framework", label: "框架管理", description: "保留 ADK 原有行为" },
    ];
  }, [runtime]);
  const [promptStatus, setPromptStatus] = useState<"idle" | "composing" | "done">("idle");
  const [promptOperation, setPromptOperation] = useState<"compose" | "optimize">("compose");
  const [createError, setCreateError] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [summaryOpen, setSummaryOpen] = useState(false);
  const [configModel, setConfigModel] = useState<ResItem | null>(null);
  const [showMcpConnect, setShowMcpConnect] = useState(false);
  const compositionRef = useRef<any>(null);
  const createRailTriggerRef = useRef<HTMLButtonElement>(null);
  const composeSeq = useRef(0);
  const conversationEntryInitialized = useRef(false);

  useEffect(() => {
    if (!contextOwnershipOptions.some(option => option.value === contextOwnership)) {
      setContextOwnership("auto");
    }
  }, [contextOwnership, contextOwnershipOptions]);

  /* conversation 模式 */
  const [convMessages, setConvMessages] = useState<Array<{ role: string; content: string }>>([]);
  const [convInput, setConvInput] = useState("");
  // Generating a Draft Patch and running the resulting Agent are separate
  // choices.  The former is one profile; the latter is an allow-list with a
  // runtime-selected latest entry as its default.
  const [convAuthoringModel, setConvAuthoringModel] = useState("");
  const [convAgentModels, setConvAgentModels] = useState<string[]>([]);
  const [convTools, setConvTools] = useState<string[]>([]);
  const [convMcp, setConvMcp] = useState<string[]>([]);
  const [convSkills, setConvSkills] = useState<string[]>([]);
  const [proposal, setProposal] = useState<any>(null);
  const conversationForm = useForm<ConversationCommitFormValues>({
    resolver: zodResolver(conversationCommitSchema) as Resolver<ConversationCommitFormValues>,
    defaultValues: {
      name: "",
      slug: generateAgentSlug(),
      runtimeType: "codex",
      prompt: "",
      description: "",
      modelProfileId: undefined,
    },
  });
  const [convBusy, setConvBusy] = useState(false);
  const [convError, setConvError] = useState("");
  const [convFallbackNotice, setConvFallbackNotice] = useState("");
  const [convStage, setConvStage] = useState<string | null>(null);
  const [convStartedAt, setConvStartedAt] = useState<number | null>(null);
  const [convReviewOpen, setConvReviewOpen] = useState(false);
  const convPollAbort = useRef<AbortController | null>(null);
  useEffect(() => () => convPollAbort.current?.abort(), []);

  /* import / project 模式 */
  const [importFile, setImportFile] = useState<File | null>(null);
  const [importInspection, setImportInspection] = useState<any>(null);
  const importForm = useForm<AgentImportFormValues>({
    resolver: zodResolver(agentImportSchema) as Resolver<AgentImportFormValues>,
    defaultValues: { name: "", slug: generateAgentSlug() },
  });
  const [projectInspection, setProjectInspection] = useState<any>(null);
  const projectForm = useForm<ProjectImportFormValues>({
    resolver: zodResolver(projectImportSchema) as Resolver<ProjectImportFormValues>,
    defaultValues: { name: "", slug: generateAgentSlug(), path: "." },
  });
  const projectPath = projectForm.watch("path");
  const [inspectBusy, setInspectBusy] = useState(false);
  const [inspectError, setInspectError] = useState("");

  const loadCatalog = useCallback(async () => {
    try {
      const [d, discovered, providerCatalog] = await Promise.all([
      apiFetch("/api/v1/catalog/resources?limit=200").then(r => r.json()),
      apiFetch("/api/v1/catalog/models").then(r => r.json()).catch(() => null),
      apiFetch("/api/v1/agent-providers").then(r => r.json()).catch(() => null),
      ]);
      const items: ResItem[] = d.items || [];
      const localModels = items.filter(i => i.kind === "model" && (i.source === "local" || i.source === "market"));
      const modelItems = discovered?.items?.length ? [...localModels, ...discovered.items] : items.filter(i => i.kind === "model");
      setCatalog([...modelItems, ...items.filter((i: ResItem) => i.kind !== "model")]);
      const providerItems: AgentProviderCatalogItem[] = providerCatalog?.items || [];
      setAgentProviders(providerItems);
      setSelectedProviderRef(current => (
        providerItems.some(item => item.providerRef === current && item.selectable)
          ? current
          : providerItems.find(item => item.selectable)?.providerRef || ""
      ));

      const references = [...new Set(
        modelItems.map(credentialReference).filter(ref => ref.startsWith("env://")),
      )];
      const statuses = await Promise.all(references.map(async ref => {
        const name = ref.slice("env://".length);
        try {
          const response = await apiFetch(`/api/v1/credentials/${encodeURIComponent(name)}`);
          return [ref, response.ok ? await response.json() : { configured: false }] as const;
        } catch {
          return [ref, { configured: false }] as const;
        }
      }));
      setCredentialStatuses(Object.fromEntries(statuses));
    } catch {
      // 保留上次成功加载的 catalog，重新进入页面时可再次拉取。
    }
  }, []);

  useEffect(() => { loadCatalog(); }, [loadCatalog]);

  const models = useMemo(
    () => catalog
      .filter(i => i.kind === "model" && ["ready", "missing-secret"].includes(i.status))
      .sort(compareStudioModels),
    [catalog],
  );
  const tools = useMemo(() => catalog.filter(i => i.kind === "tool" && i.status === "ready"), [catalog]);
  const mcps = useMemo(() => catalog.filter(i => i.kind === "mcp"), [catalog]);
  const skills = useMemo(() => catalog.filter(i => i.kind === "skill" && i.status === "ready"), [catalog]);
  const effectiveAgentProviders = useMemo<AgentProviderCatalogItem[]>(() => {
    return agentProviders;
  }, [agentProviders]);
  const selectedProvider = useMemo(
    () => effectiveAgentProviders.find(item => item.providerRef === selectedProviderRef),
    [effectiveAgentProviders, selectedProviderRef],
  );
  const usesNativeCodexTools = runtime === "codex"
    || (runtime === "plugin" && isCodexAgentProvider(selectedProviderRef));
  const effectiveSelectedTools = usesNativeCodexTools ? [] : selectedTools;
  const providerOptions = useMemo(() => effectiveAgentProviders.map(item => ({
    value: item.providerRef,
    label: item.displayName,
    description: providerOptionDescription(item),
    disabled: !item.selectable,
  })), [effectiveAgentProviders]);
  const quickRuntimeOptions = useMemo(
    () => agentProviders.some(item => item.selectable)
      ? [
          ...BUILTIN_RUNTIME_OPTIONS,
          {
            value: "plugin",
            label: agentProviders.filter(item => item.selectable).length === 1
              ? `${agentProviders.find(item => item.selectable)?.displayName || "DSH AgentProvider"} · Plugin`
              : `DSH AgentProvider · ${agentProviders.filter(item => item.selectable).length} 个可用`,
          },
        ]
      : [...BUILTIN_RUNTIME_OPTIONS],
    [agentProviders],
  );

  useEffect(() => {
    if (selectedProviderRef && effectiveAgentProviders.some(item => item.providerRef === selectedProviderRef && item.selectable)) return;
    setSelectedProviderRef(effectiveAgentProviders.find(item => item.selectable)?.providerRef || "");
  }, [effectiveAgentProviders, selectedProviderRef]);

  useEffect(() => {
    if (usesNativeCodexTools && selectedTools.length) setSelectedTools([]);
  }, [selectedTools.length, usesNativeCodexTools]);
  const resourceById = useCallback((id: string) => catalog.find(i => i.resourceId === id), [catalog]);
  const credentialOf = useCallback((item?: ResItem) => {
    const ref = credentialReference(item);
    return ref ? credentialStatuses[ref] : undefined;
  }, [credentialStatuses]);
  const hasConfiguredCredential = useCallback((item?: ResItem) => {
    if (!item) return false;
    // The catalogue and Settings share this status.  While the optional
    // per-secret probe is still in flight, a ready catalogue record is enough
    // to avoid flashing a misleading "configure credential" action.
    return credentialOf(item)?.configured ?? item.status === "ready";
  }, [credentialOf]);
  const conversationRuntime = conversationForm.watch("runtimeType");
  const conversationModelOptions = useMemo(() => models.map(item => ({
    value: item.resourceId,
    label: item.displayName,
    description: `${item.contract?.model || item.name} · ${hasConfiguredCredential(item) ? "凭证已配置" : "需配置凭证"}`,
  })), [hasConfiguredCredential, models]);
  const preferredConversationAuthoringModel = useMemo(() => {
    const preferred = models.find(item => String(item.contract?.model || item.name).toLowerCase() === "deepseek-v4-flash");
    return preferred?.resourceId || models[0]?.resourceId || "";
  }, [models]);

  useEffect(() => {
    if (mode !== "conversation") {
      conversationEntryInitialized.current = false;
      return;
    }
    if (conversationEntryInitialized.current || !preferredConversationAuthoringModel) return;
    conversationEntryInitialized.current = true;
    if (!convAuthoringModel) setConvAuthoringModel(preferredConversationAuthoringModel);
    if (!convAgentModels.length) setConvAgentModels([preferredConversationAuthoringModel]);
  }, [
    convAgentModels.length,
    convAuthoringModel,
    mode,
    preferredConversationAuthoringModel,
  ]);

  function updateConversationAgentModels(next: string[]) {
    // Preserve the catalog's newest-first order rather than click order.  The
    // first binding is the actual runtime default, so the model declaration
    // has one unambiguous source of truth.
    const selected = new Set(next);
    const ordered = models
      .map(item => item.resourceId)
      .filter(resourceId => selected.has(resourceId));
    setConvAgentModels(ordered);
    conversationForm.setValue("modelProfileId", ordered[0] || "", { shouldDirty: true, shouldValidate: true });
  }

  /* 草稿（localStorage） */
  function draftKey() { return `${DRAFT_PREFIX}:local-workspace`; }
  function saveDraft() {
    try {
      window.localStorage.setItem(draftKey(), JSON.stringify({
        version: 1, savedAt: new Date().toISOString(), mode,
        wizard: {
          step, maxStep, template, runtime, depth, selectedTools, selectedSkills,
          selectedMcp, selectedModels, policy, contextOwnership,
          contextEngineRollout, memoryEnabled, memoryWriteRollout,
          selectedProviderRef, providerConfigText,
          providerPermissionsApproved,
        },
        fields: { name, slug, description, prompt, audience, language, format, systemPrompt, taskPrompt, buildAfterCreate },
      }));
      setDraftState(`已保存 ${new Intl.DateTimeFormat("zh-CN", { hour: "2-digit", minute: "2-digit" }).format(new Date())}`);
    } catch { /* 存储不可用时静默 */ }
  }
  function markDirty() { setDraftState("有未保存更改"); }

  /* 向导 compose */
  const wizardPayload = useCallback(() => ({
    // ``prompt`` is the final system-prompt compatibility field.  Quick
    // authoring owns only an intent brief here, so let the template compile
    // ``goal`` into an editable structured prompt instead of echoing it.
    prompt: "",
    goal: prompt,
    description,
    taskPrompt,
    audience,
    language,
    depth,
    outputFormat: format,
    modelProfileId: selectedModels[0] || null,
    modelProfileIds: selectedModels,
    toolResourceIds: effectiveSelectedTools,
    skillResourceIds: selectedSkills,
    mcpResourceIds: selectedMcp,
    policyTemplate: policy,
    executionStrategy: template === "research" ? "plan-act-observe" : "direct",
    maxSteps: template === "research" ? 28 : 12,
    timeoutSeconds: template === "research" ? 900 : 120,
  }), [prompt, description, taskPrompt, template, audience, language, depth, format, selectedModels, effectiveSelectedTools, selectedSkills, selectedMcp, policy]);

  const composeAgent = useCallback(async ({ preservePrompt = true } = {}) => {
    const seq = ++composeSeq.current;
    setPromptOperation("compose");
    setPromptStatus("composing");
    try {
      const res = await apiFetch(`/api/v1/agent-templates/${template}:compose`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(wizardPayload()),
      });
      const composition = await res.json();
      if (!res.ok) {
        throw new Error(composition?.error?.message || `生成 Agent 配置失败（${res.status}）`);
      }
      if (seq !== composeSeq.current) return;
      compositionRef.current = composition;
      const b = composition.spec?.bindings || {};
      setSelectedTools(usesNativeCodexTools ? [] : (b.tools || []).map((i: any) => i.resourceId));
      setSelectedSkills((b.skills || []).map((i: any) => i.resourceId));
      setSelectedMcp((b.mcpServers || []).map((i: any) => i.resourceId));
      const ids = b.modelProfileIds?.length ? b.modelProfileIds : b.modelProfileId ? [b.modelProfileId] : [];
      if (ids.length) setSelectedModels(ids);
      if (!preservePrompt || !systemPrompt.trim()) {
        quickForm.setValue("systemPrompt", composition.spec?.instructions?.system || prompt.trim(), { shouldDirty: true });
      }
      if (!preservePrompt || !taskPrompt.trim()) {
        quickForm.setValue("taskPrompt", composition.spec?.instructions?.task || "", { shouldDirty: true });
      }
      setPromptStatus("done");
    } catch (error: any) {
      if (seq === composeSeq.current) {
        setPromptStatus("idle");
        setCreateError(error.message || "生成 Agent 配置失败");
      }
    }
  }, [template, wizardPayload, usesNativeCodexTools, systemPrompt, taskPrompt, quickForm]);

  const optimizePromptWithModel = useCallback(async () => {
    const authoringModel = selectedModels[0];
    if (!authoringModel) {
      setCreateError("请先选择用于优化 Prompt 的模型。");
      return;
    }
    const seq = ++composeSeq.current;
    setCreateError("");
    setPromptOperation("optimize");
    setPromptStatus("composing");
    try {
      const optimizationBrief = [
        "请在不改变业务目标、Runtime 和已选能力的前提下，重写并增强这个 Agent 的角色与任务契约。",
        "system 必须明确角色、目标、事实边界、失败处理和回答原则；task 必须明确每次请求的执行步骤、约束和交付结构。",
        "不要返回解释，只生成可审查的 Agent Draft Patch。",
        `Agent 名称：${name.trim() || "未命名 Agent"}`,
        `业务描述：${description.trim() || "未填写"}`,
        `原始目标：${prompt.trim()}`,
        `当前角色与系统提示词：${systemPrompt.trim() || "未生成"}`,
        `当前任务契约：${taskPrompt.trim() || "未生成"}`,
      ].join("\n\n");
      const requestId = `quick-optimize-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
      const res = await apiFetch("/api/v1/authoring/conversations:compose", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          messages: [{ role: "user", content: optimizationBrief }],
          modelProfileId: authoringModel,
          runtimeType: runtime === "plugin" ? "codex" : runtime,
          agentModelProfileIds: selectedModels,
          agentDefaultModelProfileId: authoringModel,
          toolResourceIds: effectiveSelectedTools,
          mcpResourceIds: selectedMcp,
          skillResourceIds: selectedSkills,
          requestId,
        }),
      });
      const result = await res.json().catch(() => null);
      if (!res.ok) {
        throw new Error(result?.error?.message || `优化 Prompt 失败（${res.status}）`);
      }
      if (seq !== composeSeq.current) return;
      if (result?.fallback?.active) {
        throw new Error("生成模型暂时不可用，已保留当前角色与任务契约，请稍后重试。");
      }
      const proposalSpec = result?.proposal?.spec || {};
      const improvedSystem = String(
        proposalSpec.instructions?.system || result?.proposal?.instructions?.system || "",
      ).trim();
      const improvedTask = String(
        proposalSpec.instructions?.task || result?.proposal?.instructions?.task || "",
      ).trim();
      if (!improvedSystem || !improvedTask) {
        throw new Error("生成模型没有同时返回角色提示词与任务契约，已保留当前内容。");
      }
      quickForm.setValue("systemPrompt", improvedSystem, { shouldDirty: true, shouldValidate: true });
      quickForm.setValue("taskPrompt", improvedTask, { shouldDirty: true, shouldValidate: true });
      if (compositionRef.current?.spec) {
        compositionRef.current = {
          ...compositionRef.current,
          spec: {
            ...compositionRef.current.spec,
            instructions: { system: improvedSystem, task: improvedTask },
          },
        };
      }
      markDirty();
      setPromptStatus("done");
    } catch (error: any) {
      if (seq === composeSeq.current) {
        setPromptStatus("done");
        setCreateError(error.message || "优化 Prompt 失败，已保留当前内容。");
      }
    }
  }, [
    description,
    name,
    prompt,
    quickForm,
    runtime,
    selectedMcp,
    selectedModels,
    selectedSkills,
    effectiveSelectedTools,
    systemPrompt,
    taskPrompt,
  ]);

  async function gotoStep(next: number) {
    if (next < 1 || next > 4) return;
    if (next > step) {
      if (step === 1) {
        const valid = await quickForm.trigger(["name", "slug", "runtimeType", "prompt", "audience"], { shouldFocus: true });
        if (!valid) { setCreateError("请修正标记字段后继续。"); return; }
        if (runtime === "plugin") {
          if (!selectedProvider?.selectable) {
            setCreateError(selectedProvider?.reason?.message || "请先在插件中心安装并启用一个兼容的 AgentProvider。");
            return;
          }
          try {
            parseProviderConfig(providerConfigText, selectedProvider.secretFields);
          } catch (error: any) {
            setCreateError(error.message || "Provider 配置无效");
            return;
          }
          if (selectedProvider.permissions.length && !providerPermissionsApproved) {
            setCreateError("请先确认 AgentProvider 请求的权限。");
            return;
          }
        }
      }
      if (step === 2 && !selectedModels.length) {
        setCreateError("请至少选择一个模型后继续。");
        return;
      }
    }
    setCreateError("");
    if (next === 3 && promptStatus === "idle") composeAgent({ preservePrompt: true });
    setStep(next);
    setMaxStep(m => Math.max(m, next));
    markDirty();
  }

  async function submitWizard(values: QuickAgentFormValues) {
    setCreateError("");
    setSubmitting(true);
    try {
      if (!compositionRef.current) await composeAgent({ preservePrompt: false });
      if (!compositionRef.current) {
        throw new Error("未能生成 Agent 配置，请检查模板和能力绑定后重试。");
      }
      const spec = JSON.parse(JSON.stringify(compositionRef.current?.spec || {}));
      spec.instructions = { system: values.systemPrompt.trim(), task: values.taskPrompt.trim() };
      spec.description = values.description.trim() || spec.description;
      spec.context = {
        ...(spec.context || {}),
        ownership: contextOwnership,
        promptOwnership: contextOwnership === "ksadk"
          ? "ksadk"
          : contextOwnership === "framework"
            ? "framework"
            : spec.context?.promptOwnership || "framework",
        rollout: {
          ...(spec.context?.rollout || {}),
          contextEngine: contextEngineRollout,
          memoryWrite: memoryEnabled ? memoryWriteRollout : "off",
        },
      };
      spec.memory = {
        ...(spec.memory || {}),
        enabled: memoryEnabled,
        recall: { ...(spec.memory?.recall || {}), enabled: memoryEnabled },
      };
      if (values.runtimeType === "plugin") {
        if (!selectedProvider?.selectable) {
          throw new Error(selectedProvider?.reason?.message || "所选 AgentProvider 当前不可用");
        }
        if (selectedProvider.permissions.length && !providerPermissionsApproved) {
          throw new Error("请先确认 AgentProvider 请求的权限");
        }
        spec.runtime = {
          type: "plugin",
          providerRef: selectedProvider.providerRef,
          providerConfig: parseProviderConfig(providerConfigText, selectedProvider.secretFields),
        };
        spec.security = {
          ...(spec.security || {}),
          allowedPermissions: [...new Set([
            ...(spec.security?.allowedPermissions || []),
            ...selectedProvider.permissions,
          ])].sort(),
        };
      }
      if (usesNativeCodexTools) {
        spec.bindings = { ...(spec.bindings || {}), tools: [] };
        spec.capabilities = { ...(spec.capabilities || {}), tools: [] };
      }
      const res = await apiFetch("/api/v1/authoring/quick", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          name: values.name.trim(),
          slug: values.slug.trim(),
          runtimeType: values.runtimeType,
          description: spec.description,
          template: values.template,
          spec,
        }),
      });
      const d = await res.json().catch(() => null);
      if (!res.ok) {
        setCreateError(d?.error?.message || `创建失败（${res.status}）`);
        if (applyApiFieldErrors(d, quickForm.setError)) {
          return;
        }
        throw new Error(d?.error?.message || `创建失败（${res.status}）`);
      }
      const createdId = String(d?.metadata?.id || "");
      if (!createdId) throw new Error("创建响应未返回 Agent 标识");
      if (values.buildAfterCreate) {
        const revision = Number(d?.metadata?.revision || 1);
        const buildResponse = await apiFetch(`/api/v1/agents/${encodeURIComponent(createdId)}/builds`, {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "Idempotency-Key": `build-${createdId}-r${revision}-${Date.now()}`,
          },
          body: JSON.stringify({ revision, runEvaluation: false }),
        });
        const operation = await buildResponse.json().catch(() => null);
        if (!buildResponse.ok) {
          throw new Error(operation?.error?.message || `构建提交失败（${buildResponse.status}）`);
        }
        const operationId = String(operation?.id || "");
        if (!operationId) throw new Error("构建响应未返回操作标识");
        await waitForCreatedBuild(operationId);
      }
      try {
        window.localStorage.removeItem(draftKey());
      } catch {
        // 隐私模式下 localStorage 可能不可用；创建和进入会话不能因此失败。
      }
      onCreated(createdId, values.buildAfterCreate);
    } catch (e: any) {
      setCreateError(e.message || "创建失败");
    } finally {
      setSubmitting(false);
    }
  }

  /* conversation 模式 */
  async function sendConversation() {
    const input = convInput.trim();
    if (!input || !convAuthoringModel || !convAgentModels.length) {
      setConvError("请输入需求并选择用于构建的模型。");
      return;
    }
    if (convBusy) return; // 创建中禁止重复提交
    setConvError("");
    setConvFallbackNotice("");
    const next = [...convMessages, { role: "user", content: input }];
    setConvMessages(next);
    setConvInput("");
    setConvBusy(true);
    setConvStartedAt(Date.now());
    const requestId = `conv-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
    setConvStage("resolving_model");
    const poller = pollConversationStages(requestId);
    try {
      const res = await apiFetch("/api/v1/authoring/conversations:compose", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          messages: next,
          // The authoring model is a single request-scoped choice.  The
          // multi-model Agent contract is carried separately and injected by
          // the server after validating every profile.
          modelProfileId: convAuthoringModel,
          runtimeType: conversationRuntime,
          agentModelProfileIds: convAgentModels,
          agentDefaultModelProfileId: convAgentModels[0] || null,
          toolResourceIds: conversationRuntime === "codex" ? [] : convTools,
          mcpResourceIds: convMcp,
          skillResourceIds: convSkills,
          requestId,
        }),
      });
      const d = await res.json();
      if (!res.ok) throw new Error(d?.error?.message || `生成失败（${res.status}）`);
      setConvStage("done");
      if (d?.fallback?.active) {
        setConvFallbackNotice(
          "所选生成模型暂时未能返回可用草稿。Studio 已按当前对话和你选定的 Runtime、模型与能力资源生成可编辑的本地兜底草稿；确认创建前请检查并补全。",
        );
      }
      setProposal(d.proposal);
      // A fresh turn updates the compact rail; the editable form stays hidden
      // until the user explicitly decides to inspect and confirm the patch.
      setConvReviewOpen(false);
      const proposalSpec = d.proposal.spec || {
        description: d.proposal.description || "",
        instructions: d.proposal.instructions || {},
      };
      conversationForm.reset({
        name: d.proposal.name || "",
        slug: generateAgentSlug(),
        // Runtime is selected by Studio and enforced again by the server.
        // Never let a model response replace the user's deployment target.
        runtimeType: conversationRuntime,
        prompt: proposalSpec.instructions?.system || "",
        description: proposalSpec.description || d.proposal.description || "",
        modelProfileId: proposalSpec.bindings?.modelProfileId || convAgentModels[0],
      });
      setConvMessages([
        ...next,
        { role: "assistant", content: JSON.stringify(compactConversationProposal(d.proposal)) },
      ]);
    } catch (e: any) {
      setConvStage("failed");
      setConvError(e.message || "对话构建失败");
    } finally {
      convPollAbort.current?.abort();
      convPollAbort.current = null;
      void poller;
      setConvBusy(false);
    }
  }

  /* 轮询后端构建阶段（resolving_model → generating → validating → correcting → done/failed），
   * 只驱动等待文案，不解析内容；失败时静默停止。 */
  function pollConversationStages(requestId: string) {
    const controller = new AbortController();
    convPollAbort.current?.abort();
    convPollAbort.current = controller;
    const poll = async () => {
      while (!controller.signal.aborted) {
        await new Promise(resolve => window.setTimeout(resolve, 800));
        if (controller.signal.aborted) return;
        try {
          const response = await apiFetch(
            `/api/v1/authoring/conversations:status/${encodeURIComponent(requestId)}`,
            { signal: controller.signal },
          );
          if (!response.ok) continue;
          const status = await response.json();
          if (status?.stage) setConvStage(String(status.stage));
        } catch {
          return; // 轮询失败不打断主请求，静默停止
        }
      }
    };
    void poll();
    return controller;
  }

  async function confirmConversation(values: ConversationCommitFormValues) {
    if (!proposal) return;
    setConvBusy(true);
    setConvError("");
    try {
      const proposalSpec = proposal.spec || {
        description: proposal.description || "",
        instructions: proposal.instructions || {},
      };
      const defaultModel = convAgentModels[0] || values.modelProfileId || null;
      const allowedModels = convAgentModels.length ? convAgentModels : defaultModel ? [defaultModel] : [];
      const selectedTools = values.runtimeType === "codex" ? [] : convTools;
      const spec = mergeAgentSpec(proposalSpec, {
        description: values.description?.trim() || proposalSpec.description || proposal.description || "",
        // Framework source is generated by Studio during confirmation from the
        // selected Runtime. A Draft Patch never carries a model-authored ADK
        // or LangGraph project path.
        runtime: null,
        // Persist only the validated Profile binding.  Build/run resolves the
        // live model contract; catalog metadata never belongs to Agent Drafts.
        model: null,
        instructions: {
          system: values.prompt.trim(),
          task: proposalSpec.instructions?.task || "",
        },
        bindings: {
          modelProfileId: defaultModel,
          modelProfileIds: allowedModels,
          modelParameters: null,
          policyTemplate: "strict",
          tools: selectedTools.map(resourceId => ({ resourceId })),
          mcpServers: convMcp.map(resourceId => ({ resourceId })),
          skills: convSkills.map(resourceId => ({ resourceId })),
        },
      });
      const res = await apiFetch("/api/v1/authoring/quick", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          name: values.name.trim(), slug: values.slug.trim(), runtimeType: values.runtimeType,
          description: spec.description,
          spec,
        }),
      });
      const d = await res.json().catch(() => null);
      if (!res.ok) {
        setConvError(d?.error?.message || `创建失败（${res.status}）`);
        if (applyApiFieldErrors(d, conversationForm.setError)) return;
        throw new Error(d?.error?.message || `创建失败（${res.status}）`);
      }
      onCreated(d?.metadata?.id);
    } catch (e: any) {
      setConvError(e.message || "创建失败");
    } finally {
      setConvBusy(false);
    }
  }

  /* import / project */
  async function inspectImport() {
    if (!importFile) return;
    setInspectBusy(true);
    setInspectError("");
    try {
      const body = new FormData();
      body.append("file", importFile);
      const res = await apiFetch("/api/v1/authoring/imports:inspect", { method: "POST", body });
      const d = await res.json();
      if (!res.ok) throw new Error(d?.error?.message || `检查失败（${res.status}）`);
      setImportInspection(d);
      importForm.reset({ name: d.displayName || "", slug: generateAgentSlug() });
    } catch (e: any) {
      setInspectError(e.message);
    } finally {
      setInspectBusy(false);
    }
  }

  async function commitImport(values: AgentImportFormValues) {
    if (!importInspection) return;
    setInspectBusy(true);
    setInspectError("");
    try {
      const res = await apiFetch(`/api/v1/authoring/imports/${encodeURIComponent(importInspection.inspectionToken)}:commit`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: values.name.trim(), slug: values.slug.trim() || undefined }),
      });
      const d = await res.json();
      if (!res.ok) {
        if (applyApiFieldErrors(d, importForm.setError)) return;
        throw new Error(d?.error?.message || `导入失败（${res.status}）`);
      }
      onCreated(d?.metadata?.id);
    } catch (e: any) {
      setInspectError(e.message);
    } finally {
      setInspectBusy(false);
    }
  }

  async function inspectProject() {
    if (!await projectForm.trigger("path")) return;
    setInspectBusy(true);
    setInspectError("");
    try {
      const res = await apiFetch("/api/v1/authoring/projects:inspect", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path: projectPath.trim() }),
      });
      const d = await res.json();
      if (!res.ok) throw new Error(d?.error?.message || `检测失败（${res.status}）`);
      setProjectInspection(d);
      projectForm.reset({ name: d.name || "Detected Agent", slug: generateAgentSlug(), path: projectPath });
    } catch (e: any) {
      setInspectError(e.message);
    } finally {
      setInspectBusy(false);
    }
  }

  async function commitProject(values: ProjectImportFormValues) {
    if (!projectInspection) return;
    setInspectBusy(true);
    setInspectError("");
    try {
      const res = await apiFetch(`/api/v1/authoring/projects/${encodeURIComponent(projectInspection.inspectionToken)}:commit`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: values.name.trim(), slug: values.slug.trim() || undefined, modelProfileId: models[0]?.resourceId || null }),
      });
      const d = await res.json();
      if (!res.ok) {
        if (applyApiFieldErrors(d, projectForm.setError)) return;
        throw new Error(d?.error?.message || `创建失败（${res.status}）`);
      }
      onCreated(d?.metadata?.id);
    } catch (e: any) {
      setInspectError(e.message);
    } finally {
      setInspectBusy(false);
    }
  }

  const templateLabel = template === "research" ? "深度调研" : "空白 Agent";
  const runtimeLabel = ({ codex: "Codex", adk: "ADK", langgraph: "LangGraph", plugin: "外部 Provider" } as Record<string, string>)[runtime] || runtime;
  const policyMeta = POLICY_META[policy];
  const reviewModel = selectedModels.map(id => resourceById(id)?.displayName || id).join("、") || "待选择";
  const selectedModelItems = selectedModels.map(resourceById).filter((item): item is ResItem => Boolean(item));
  const selectedModelStatus = selectedModelItems.length === 0
    ? "未选择模型；Agent 可以先构建，但运行前需要配置。"
    : selectedModelItems.every(hasConfiguredCredential)
      ? `已选 ${selectedModelItems.length} 个模型 · 凭证已配置`
      : "部分模型凭证未配置；Agent 可以先构建，但运行前需要配置 API Key。";
  const selectedModelNeedsCredential = selectedModelItems.some(
    item => !hasConfiguredCredential(item),
  );
  const isManagedRuntime = runtime === "codex";
  const wizardStepMeta = isManagedRuntime
    ? [...WIZARD_STEP_META.slice(0, 3), ["检查并创建", "校验声明与打开会话"]]
    : WIZARD_STEP_META;

  const closeCreateRail = useCallback((restoreFocus = false) => {
    setCreateRailOpen(false);
    if (restoreFocus) {
      requestAnimationFrame(() => createRailTriggerRef.current?.focus());
    }
  }, []);

  useEffect(() => {
    if (viewportMode !== "compact") closeCreateRail();
  }, [closeCreateRail, viewportMode]);

  function selectMode(nextMode: Mode) {
    setMode(nextMode);
    if (viewportMode === "compact") closeCreateRail(true);
  }

  const MODE_TABS: Array<{ id: Mode; icon: any; label: string; sub: string }> = [
    { id: "quick", icon: Zap, label: "快速创建", sub: "配置 YAML Revision" },
    { id: "conversation", icon: MessagesSquare, label: "对话构建", sub: "多轮生成 Draft Patch" },
    { id: "import", icon: Upload, label: "导入", sub: "YAML / Agent ZIP" },
    { id: "project", icon: Folder, label: "项目识别", sub: "检测 ADK / LangGraph" },
  ];
  const layout = !editingAgentId && mode === "conversation" ? "workbench" : "document";

  const renderCreateRailContent = (showModeLabel: boolean) => (
    <div className="create-rail-panel">
      {showModeLabel && <div className="create-rail-label">创建方式</div>}
      {!editingAgentId && (
        <nav className="authoring-mode-tabs" aria-label="创建方式" role="tablist">
          {MODE_TABS.map(tab => {
            const Icon = tab.icon;
            return (
              <button
                key={tab.id}
                id={`authoring-tab-${tab.id}`}
                className={mode === tab.id ? "active" : ""}
                type="button"
                role="tab"
                aria-selected={mode === tab.id}
                aria-controls={`authoring-panel-${tab.id}`}
                title={`${tab.label}：${tab.sub}`}
                onClick={() => selectMode(tab.id)}
              >
                <Icon size={16} /><span><strong>{tab.label}</strong><small>{tab.sub}</small></span>
              </button>
            );
          })}
        </nav>
      )}
      {(editingAgentId || mode === "quick") && (
        <>
          <div className="create-rail-divider" />
          <div className="create-rail-label wizard-step-label">配置步骤</div>
          <nav className="wizard-steps" aria-label="创建步骤">
            {wizardStepMeta.map((meta, index) => {
              const number = index + 1;
              const completed = !editingAgentId && number < maxStep && number !== step;
              return (
                <button
                  key={number}
                  className={`wizard-step${(editingAgentId ? number === 1 : step === number) ? " active" : ""}${completed ? " completed" : ""}`}
                  type="button"
                  disabled={Boolean(editingAgentId) || number > maxStep}
                  onClick={() => gotoStep(number)}
                >
                  <span className="step-number">{completed ? <Check size={13} strokeWidth={3} /> : number}</span>
                  <span><strong>{meta[0]}</strong><small>{meta[1]}</small></span>
                </button>
              );
            })}
          </nav>
        </>
      )}
    </div>
  );

  return (
    <div className="create-shell page-container" data-layout={layout} data-authoring-mode={mode} data-editing={editingAgentId ? "true" : "false"}>
      <PageHeaderActions>
        {!editingAgentId && viewportMode === "compact" && (
          <button
            ref={createRailTriggerRef}
            className="icon-button compact-create-rail-trigger"
            type="button"
            aria-label="查看创建入口与配置步骤"
            title="查看创建入口与配置步骤"
            aria-expanded={createRailOpen}
            aria-controls="createRail"
            onClick={() => setCreateRailOpen(open => !open)}
          >
            <PanelRight size={16} />
          </button>
        )}
        {!editingAgentId && mode === "quick" && <span className="tag">{draftState}</span>}
      </PageHeaderActions>

      <div className="create-workbench">
        {!editingAgentId && viewportMode !== "compact" && (
          <aside id="createRail" className="create-rail" aria-label="创建方式与步骤">
            {renderCreateRailContent(true)}
          </aside>
        )}

        {!editingAgentId && viewportMode === "compact" && createRailOpen && (
          <StudioDrawer
            open
            compact
            title="创建方式"
            subtitle="切换创建入口，或查看当前配置步骤。"
            onOpenChange={open => {
              if (!open) closeCreateRail(true);
            }}
          >
            <div id="createRail">{renderCreateRailContent(false)}</div>
          </StudioDrawer>
        )}

        <div className="create-stage">
          {editingAgentId && (
            <AgentEditor
              agentId={editingAgentId}
              catalog={catalog}
              providers={agentProviders}
              onSaved={(id, openChat) => onCreated(id, openChat)}
              onAppearanceSaved={onAgentsChanged}
            />
          )}
          {!editingAgentId && mode === "conversation" && (
            <section
              id="authoring-panel-conversation"
              className="authoring-mode-panel"
              role="tabpanel"
              aria-labelledby="authoring-tab-conversation"
            >
              <div className="authoring-panel-heading conversation-panel-heading">
                <div><span className="eyebrow">Conversation authoring</span><h2>对话创建 Agent</h2><p>描述目标，逐轮完善；准备好后再检查并确认草稿。</p></div>
                <span className="tag">不会自动创建</span>
              </div>
              <div
                className="conversation-authoring-layout"
                data-draft-state={!proposal ? "empty" : convReviewOpen ? "review" : "summary"}
              >
                <section className="conversation-chat" aria-label="对话创建">
                  <div className="conversation-chat-header">
                    <div><strong>从需求开始</strong><p>像对话一样说明要做什么；后续可以继续补充边界和能力。</p></div>
                    <span className="conversation-context-state">{convMessages.length ? `${Math.ceil(convMessages.length / 2)} 轮上下文` : "持续保留上下文"}</span>
                  </div>
                  <div className="conversation-transcript" aria-live="polite">
                    {convMessages.length === 0 && (
                      <div className="conversation-empty-state">
                        <Sparkles size={18} aria-hidden="true" />
                        <strong>从一句需求开始</strong>
                        <p>例如：帮我做一个销售日报 Agent，能汇总群聊记录并标出待跟进事项。</p>
                      </div>
                    )}
                    {convMessages.map((m, i) => {
                      const assistant = m.role !== "user" ? summarizeConversationProposal(m.content) : null;
                      return (
                        <div key={i} className={`conversation-message ${m.role === "user" ? "user" : "assistant"}`}>
                          {m.role === "user" ? (
                            <p>{m.content}</p>
                          ) : (
                            <><strong>{assistant?.title}</strong><p>{assistant?.body}</p></>
                          )}
                        </div>
                      );
                    })}
                    {convBusy && (
                      <div className="conversation-thinking" role="status">
                        <span className="conversation-thinking-orb" aria-hidden="true"><i /><i /><i /></span>
                        <TextShimmer stage={convStage} startedAt={convStartedAt} />
                      </div>
                    )}
                  </div>
                  {convError && <div className="inline-alert error"><CircleAlert size={16} /><div><strong>对话构建失败</strong><p>{convError}</p></div></div>}
                  {convFallbackNotice && <div className="inline-alert warning" role="status"><CircleAlert size={16} /><div><strong>已生成本地兜底草稿</strong><p>{convFallbackNotice}</p></div></div>}
                  <form className="conversation-composer" onSubmit={event => { event.preventDefault(); void sendConversation(); }}>
                    <textarea
                      rows={3}
                      placeholder="描述你想创建或调整的 Agent…"
                      value={convInput}
                      onChange={event => setConvInput(event.target.value)}
                      onKeyDown={event => {
                        const nativeEvent = event.nativeEvent;
                        if (event.key === "Enter" && !event.shiftKey && !nativeEvent.isComposing && nativeEvent.keyCode !== 229) {
                          event.preventDefault();
                          void sendConversation();
                        }
                      }}
                    />
                    <div className="conversation-composer-footer">
                      <span>Enter 发送 <b>·</b> Shift + Enter 换行</span>
                      <button className="conversation-send-button" type="submit" disabled={convBusy || !convInput.trim()} aria-label={convBusy ? "正在生成" : "生成方案"} title={convBusy ? "正在生成" : "生成方案"}>
                        <Send size={16} aria-hidden="true" />
                      </button>
                    </div>
                  </form>
                  <details className="conversation-settings">
                    <summary>
                      <span>部署配置</span>
                      <small>{RUNTIME_OPTIONS.find(option => option.value === conversationRuntime)?.label.split(" · ")[0] || "Runtime"} · {convAgentModels.length || 0} 个模型 · {convSkills.length + convMcp.length + (conversationRuntime === "codex" ? 0 : convTools.length)} 项能力</small>
                    </summary>
                    <div className="conversation-settings-body">
                      <FormField
                        label="生成模型 Profile"
                        className="authoring-model-field"
                        footer={<span>只决定本次如何生成草稿；默认 DeepSeek V4 Flash。</span>}
                      >
                        <StudioSelect
                          ariaLabel="选择用于生成草稿的模型"
                          value={convAuthoringModel}
                          options={conversationModelOptions}
                          onValueChange={setConvAuthoringModel}
                        />
                      </FormField>
                      <FormField label="Runtime" requirement="required" htmlFor="conversationRuntime" error={conversationForm.formState.errors.runtimeType?.message}>
                        <StudioSelect
                          id="conversationRuntime"
                          ariaLabel="Runtime"
                          value={conversationRuntime}
                          options={RUNTIME_OPTIONS}
                          onValueChange={value => conversationForm.setValue("runtimeType", value as ConversationCommitFormValues["runtimeType"], { shouldDirty: true, shouldValidate: true })}
                        />
                      </FormField>
                      <FormField
                        label="Agent 可用模型"
                        className="authoring-model-field"
                        footer={<span>可多选；按目录中最新的模型作为运行默认值。</span>}
                      >
                        <StudioMultiSelect
                          ariaLabel="选择对话 Agent 模型"
                          items={models}
                          selectedIds={convAgentModels}
                          getId={item => item.resourceId}
                          getLabel={item => item.displayName}
                          getDescription={item => `${item.contract?.model || item.name} · ${hasConfiguredCredential(item) ? "凭证已配置" : "需配置凭证"}`}
                          onChange={updateConversationAgentModels}
                          searchPlaceholder="搜索 Agent 模型"
                          emptyMessage="没有可用模型"
                        />
                      </FormField>
                      <FormField label="Skill" className="authoring-model-field">
                        <StudioMultiSelect
                          ariaLabel="选择对话 Agent Skill"
                          items={skills}
                          selectedIds={convSkills}
                          getId={item => item.resourceId}
                          getLabel={item => item.displayName}
                          getDescription={item => `${item.version} · ${item.description || "版本化 Skill"}`}
                          onChange={setConvSkills}
                          searchPlaceholder="搜索 Skill"
                          emptyMessage="没有已安装的 Skill"
                        />
                      </FormField>
                      <FormField label="MCP Server" className="authoring-model-field">
                        <StudioMultiSelect
                          ariaLabel="选择对话 Agent MCP Server"
                          items={mcps}
                          selectedIds={convMcp}
                          disabledIds={mcps.filter(item => !convMcp.includes(item.resourceId) && mcpUnavailableReason(item, conversationRuntime)).map(item => item.resourceId)}
                          getId={item => item.resourceId}
                          getLabel={item => item.displayName}
                          getDescription={item => mcpUnavailableReason(item, conversationRuntime) || `${item.description || "MCP Server"} · ${item.health?.toolCount || 0} Tool`}
                          onChange={setConvMcp}
                          searchPlaceholder="搜索 MCP Server"
                          emptyMessage="没有已连接的 MCP Server"
                        />
                      </FormField>
                      {conversationRuntime === "codex" ? (
                        <p className="helper conversation-runtime-note">Codex 使用原生工具、MCP 和 Skill；KsADK Tool 仅绑定到 ADK / LangGraph 通用 Agent。</p>
                      ) : (
                        <FormField label="KsADK Tool" className="authoring-model-field">
                          <StudioMultiSelect
                            ariaLabel="选择对话 Agent Tool"
                            items={tools}
                            selectedIds={convTools}
                            getId={item => item.resourceId}
                            getLabel={item => item.displayName}
                            getDescription={item => `${item.version} · ${item.description || "本地 Tool"}`}
                            onChange={setConvTools}
                            searchPlaceholder="搜索 Tool"
                            emptyMessage="没有可用 Tool"
                          />
                        </FormField>
                      )}
                    </div>
                  </details>
                </section>
                <FormProvider {...conversationForm}>
                <aside className={`conversation-draft-rail${!proposal ? " is-empty" : convReviewOpen ? " is-reviewing" : ""}`} aria-label="Draft Patch">
                  <div className="conversation-draft-rail-heading">
                    <div><strong>Draft Patch</strong><p>{proposal ? "草稿已随对话更新" : "对话后生成，可随时检查"}</p></div>
                    <span className="badge" data-state={proposal ? "ready" : "pending"}>{proposal ? "已更新" : "待生成"}</span>
                  </div>
                  {!proposal ? (
                    <div className="conversation-draft-empty">
                      <Bot size={20} aria-hidden="true" />
                      <strong>从对话开始</strong>
                      <p>先描述目标。草稿会在这里显示摘要，不会自动创建 Agent。</p>
                    </div>
                  ) : !convReviewOpen ? (
                    <div className="conversation-draft-summary">
                      <div className="conversation-draft-title"><Bot size={18} aria-hidden="true" /><div><strong>{conversationForm.watch("name") || proposal.name}</strong><p>{conversationForm.watch("description") || proposal.description || "已生成可编辑的 Agent 草稿"}</p></div></div>
                      <div className="conversation-preview-section">
                        <span>角色与系统提示词</span>
                        <p>{conversationForm.watch("prompt") || proposal?.spec?.instructions?.system || "尚未生成"}</p>
                      </div>
                      <div className="conversation-preview-section">
                        <span>任务契约</span>
                        <p>{proposal?.spec?.instructions?.task || "根据对话目标完成任务。"}</p>
                      </div>
                      <div className="conversation-draft-tags">
                        <span>{RUNTIME_OPTIONS.find(option => option.value === conversationRuntime)?.label.split(" · ")[0] || "Runtime"}</span>
                        <span>{convAgentModels.length} 个模型</span>
                        {(convSkills.length + convMcp.length + (conversationRuntime === "codex" ? 0 : convTools.length)) > 0 && <span>{convSkills.length + convMcp.length + (conversationRuntime === "codex" ? 0 : convTools.length)} 项能力</span>}
                      </div>
                      <button className="button accent" type="button" onClick={() => setConvReviewOpen(true)}><PanelRight size={16} /><span>编辑并创建</span></button>
                    </div>
                  ) : (
                    <form className="conversation-review-form" onSubmit={conversationForm.handleSubmit(confirmConversation)} noValidate>
                      <div className="conversation-review-heading"><div><strong>检查并确认</strong><p>编辑名称、提示词或打开左侧部署配置；确认后才会创建 Revision。</p></div><button type="button" className="button secondary" onClick={() => setConvReviewOpen(false)}>收起</button></div>
                      <div className="form-grid two-columns">
                        <FormField label="显示名称" requirement="required" htmlFor="conversationName" error={conversationForm.formState.errors.name?.message}>
                          <input id="conversationName" {...conversationForm.register("name")} />
                        </FormField>
                        <GeneratedIdField
                          id="conversationSlug"
                          value={conversationForm.watch("slug")}
                          onChange={value => conversationForm.setValue("slug", value, { shouldDirty: true, shouldValidate: true })}
                          error={conversationForm.formState.errors.slug?.message}
                        />
                      </div>
                      <FormField label="系统提示词" requirement="required" htmlFor="conversationPrompt" error={conversationForm.formState.errors.prompt?.message}>
                        <textarea id="conversationPrompt" rows={8} {...conversationForm.register("prompt")} />
                      </FormField>
                      <div className="authoring-card-actions">
                        <button className="button accent" type="submit" disabled={convBusy}><Check size={16} /><span>确认并创建 Revision</span></button>
                      </div>
                    </form>
                  )}
                </aside>
                </FormProvider>
              </div>
            </section>
          )}

          {!editingAgentId && mode === "import" && (
            <section
              id="authoring-panel-import"
              className="authoring-mode-panel"
              role="tabpanel"
              aria-labelledby="authoring-tab-import"
            >
              <div className="authoring-panel-heading">
                <div><span className="eyebrow">Agent import</span><h2>检查并导入 Agent</h2><p>先解析格式、Runtime、文件清单和 SHA-256，确认后再写入。</p></div>
                <span className="tag">YAML / ZIP</span>
              </div>
              <div className="authoring-inspect-grid">
                <form className="authoring-input-card" onSubmit={e => { e.preventDefault(); inspectImport(); }}>
                  <div className="authoring-section-heading">
                    <span className="authoring-section-index">01</span>
                    <div><strong>选择 Agent 文件</strong><p>支持 YAML 或 ZIP；检查过程不会写入 Catalog。</p></div>
                  </div>
                  <FormField label="Agent 文件" requirement="required" hint="拖放 Agent YAML / ZIP，或点击选择">
                    <div>
                    <FileDropzone
                      ariaLabel="选择 Agent YAML 或 ZIP"
                      accept={{
                        "application/zip": [".zip"],
                        "application/yaml": [".yaml", ".yml"],
                        "text/yaml": [".yaml", ".yml"],
                      }}
                      maxSize={100 * 1024 * 1024}
                      file={importFile}
                      onFile={file => { setImportFile(file); setImportInspection(null); }}
                      onError={setInspectError}
                    />
                    </div>
                  </FormField>
                  <div className="authoring-card-actions">
                    <button className="button accent" type="submit" disabled={inspectBusy}><Search size={16} /><span>{inspectBusy ? "检查中" : "只读检查"}</span></button>
                  </div>
                </form>
                <FormProvider {...importForm}>
                <form className="authoring-inspection-card" onSubmit={importForm.handleSubmit(commitImport)} noValidate>
                  <div className="authoring-section-heading">
                    <span className="authoring-section-index">02</span>
                    <div><strong>检查并确认</strong><p>核对解析结果、警告与 RuntimeRef，再执行导入。</p></div>
                    <span className="badge" data-state={importInspection ? "ready" : "pending"} aria-live="polite">
                      {importInspection ? "检查完成" : "等待检查"}
                    </span>
                  </div>
                  <div className="form-grid two-columns">
                    <FormField label="显示名称" requirement="required" htmlFor="importName" error={importForm.formState.errors.name?.message}>
                      <input id="importName" {...importForm.register("name")} />
                    </FormField>
                    <GeneratedIdField
                      id="importSlug"
                      value={importForm.watch("slug")}
                      onChange={value => importForm.setValue("slug", value, { shouldDirty: true, shouldValidate: true })}
                      error={importForm.formState.errors.slug?.message}
                    />
                  </div>
                  <CodeViewer
                    code={importInspection ? JSON.stringify(importInspection, null, 2) : "选择文件并检查后显示解析结果、警告和 RuntimeRef。"}
                    language={importInspection ? "json" : "text"}
                    filename="agent-import-inspection.json"
                    showLineNumbers={Boolean(importInspection)}
                    wrap={!importInspection}
                  />
                  <div className="authoring-card-actions">
                    <button className="button accent" type="submit" disabled={!importInspection || inspectBusy}><Check size={16} /><span>确认导入</span></button>
                  </div>
                </form>
                </FormProvider>
              </div>
              {inspectError && <div className="inline-alert error"><CircleAlert size={16} /><div><strong>操作失败</strong><p>{inspectError}</p></div></div>}
            </section>
          )}

          {!editingAgentId && mode === "project" && (
            <section
              id="authoring-panel-project"
              className="authoring-mode-panel"
              role="tabpanel"
              aria-labelledby="authoring-tab-project"
            >
              <div className="authoring-panel-heading">
                <div><span className="eyebrow">Project detection</span><h2>识别现有项目</h2><p>复用 FrameworkDetector 展示证据和置信度；确认前不修改源码。</p></div>
                <span className="tag">Workspace only</span>
              </div>
              <FormProvider {...projectForm}>
              <div className="authoring-inspect-grid">
                <form className="authoring-input-card" onSubmit={e => { e.preventDefault(); inspectProject(); }}>
                  <div className="authoring-section-heading">
                    <span className="authoring-section-index">01</span>
                    <div><strong>指定项目路径</strong><p>仅识别当前工作区内的目录，不会修改项目源码。</p></div>
                  </div>
                  <FormField label="工作区相对路径" requirement="required" htmlFor="projectPath" hint="仅检查当前工作区内的目录，不会修改项目源码。" error={projectForm.formState.errors.path?.message}>
                    <input id="projectPath" {...projectForm.register("path")} />
                  </FormField>
                  <div className="authoring-card-actions">
                    <button className="button accent" type="submit" disabled={inspectBusy}><Search size={16} /><span>{inspectBusy ? "检测中" : "检测项目"}</span></button>
                  </div>
                </form>
                <form className="authoring-inspection-card" onSubmit={projectForm.handleSubmit(commitProject)} noValidate>
                  <div className="authoring-section-heading">
                    <span className="authoring-section-index">02</span>
                    <div><strong>检查并确认</strong><p>核对 FrameworkDetector 证据与置信度，再创建 Revision。</p></div>
                    <span className="badge" data-state={projectInspection ? "ready" : "pending"} aria-live="polite">
                      {projectInspection ? "检测完成" : "等待检测"}
                    </span>
                  </div>
                  <div className="form-grid two-columns">
                    <FormField label="显示名称" requirement="required" htmlFor="projectName" error={projectForm.formState.errors.name?.message}>
                      <input id="projectName" {...projectForm.register("name")} />
                    </FormField>
                    <GeneratedIdField
                      id="projectSlug"
                      value={projectForm.watch("slug")}
                      onChange={value => projectForm.setValue("slug", value, { shouldDirty: true, shouldValidate: true })}
                      error={projectForm.formState.errors.slug?.message}
                    />
                  </div>
                  <CodeViewer
                    code={projectInspection ? JSON.stringify(projectInspection, null, 2) : "输入本地项目路径后显示 FrameworkDetector 证据。"}
                    language={projectInspection ? "json" : "text"}
                    filename="project-inspection.json"
                    showLineNumbers={Boolean(projectInspection)}
                    wrap={!projectInspection}
                  />
                  <div className="authoring-card-actions">
                    <button className="button accent" type="submit" disabled={!projectInspection || inspectBusy}><Check size={16} /><span>确认创建 Revision</span></button>
                  </div>
                </form>
              </div>
              </FormProvider>
              {inspectError && <div className="inline-alert error"><CircleAlert size={16} /><div><strong>操作失败</strong><p>{inspectError}</p></div></div>}
            </section>
          )}

          {!editingAgentId && mode === "quick" && (
            <div
              id="authoring-panel-quick"
              className="wizard-layout"
              role="tabpanel"
              aria-labelledby="authoring-tab-quick"
            >
              <FormProvider {...quickForm}>
              <form id="quickAgentForm" className="wizard-content" onSubmit={quickForm.handleSubmit(submitWizard)} noValidate>
                {/* 第 1 步：定义 Agent */}
                <section className={`wizard-panel${step === 1 ? " active" : ""}`} hidden={step !== 1}>
                  <div className="panel-heading">
                    <span className="panel-index">01</span>
                    <div><h2>定义 Agent 的角色</h2><p>选择起点，并说明 Agent 的职责、边界和期望行为。</p></div>
                  </div>
                  <div className="field">
                    <label>创建方式</label>
                    <div className="template-grid">
                      <button className={`template-card${template === "blank" ? " selected" : ""}`} type="button" onClick={() => { quickForm.setValue("template", "blank", { shouldDirty: true }); markDirty(); }}>
                        <span className="template-icon"><Bot size={18} /></span>
                        <span><strong>空白 Agent</strong><small>输入系统提示词，自主选择能力和执行策略</small></span>
                        <span className="choice-check"><Check size={14} /></span>
                      </button>
                      <button className={`template-card${template === "research" ? " selected" : ""}`} type="button" onClick={() => { quickForm.setValue("template", "research", { shouldDirty: true }); markDirty(); }}>
                        <span className="template-icon"><Search size={18} /></span>
                        <span><strong>深度调研</strong><small>预置问题拆解、来源验证和引用报告方法</small></span>
                        <span className="choice-check"><Check size={14} /></span>
                      </button>
                    </div>
                  </div>
                  <div className="form-grid two-columns">
                    <FormField label="Agent 名称" requirement="required" htmlFor="quickAgentName" error={quickForm.formState.errors.name?.message}>
                      <input id="quickAgentName" maxLength={128} placeholder="例如：技术支持助手" {...quickForm.register("name", { onChange: markDirty })} />
                    </FormField>
                    <GeneratedIdField
                      value={slug}
                      onChange={value => {
                        quickForm.setValue("slug", value, { shouldDirty: true, shouldValidate: true });
                        markDirty();
                      }}
                      error={quickForm.formState.errors.slug?.message}
                    />
                  </div>
                  <FormField label="Runtime" requirement="required" htmlFor="quickRuntime" hint="由所选 RuntimeAdapter 运行；Codex 支持 OpenAI Responses 与兼容代理。" error={quickForm.formState.errors.runtimeType?.message}>
                    <StudioSelect
                      id="quickRuntime"
                      ariaLabel="Runtime"
                      value={runtime}
                      options={quickRuntimeOptions}
                      onValueChange={value => {
                        quickForm.setValue("runtimeType", value as QuickAgentFormValues["runtimeType"], { shouldDirty: true, shouldValidate: true });
                        markDirty();
                      }}
                    />
                  </FormField>
                  {runtime === "plugin" && (
                    <div className="template-specific" data-testid="external-provider-config">
                      <FormField
                        label="AgentProvider"
                        requirement="required"
                        htmlFor="quickAgentProvider"
                        hint="选项来自本机已安装的 agent.provider/v1；不可用版本会保留原因但不能选择。"
                      >
                        <StudioSelect
                          id="quickAgentProvider"
                          ariaLabel="AgentProvider"
                          value={selectedProviderRef}
                          placeholder="没有可用的 AgentProvider"
                          options={providerOptions}
                          disabled={!providerOptions.some(option => !option.disabled)}
                          onValueChange={value => {
                            setSelectedProviderRef(value);
                            setProviderPermissionsApproved(false);
                            markDirty();
                          }}
                        />
                      </FormField>
                      {!selectedProvider?.selectable && (
                        <div className="inline-alert warning" role="status">
                          <CircleAlert size={16} />
                          <div>
                            <strong>AgentProvider 当前不可用</strong>
                            <p>{selectedProvider?.reason?.message || "请先在插件中心安装并启用兼容的 AgentProvider。"}</p>
                          </div>
                        </div>
                      )}
                      <FormField
                        label="Provider 配置"
                        requirement="optional"
                        htmlFor="quickProviderConfig"
                        hint="填写 JSON 对象；密码、Token、API Key 只能使用 env://、secret://、credential:// 或 vault:// 引用。"
                      >
                        <textarea
                          id="quickProviderConfig"
                          className="mono"
                          rows={5}
                          value={providerConfigText}
                          onChange={event => { setProviderConfigText(event.target.value); markDirty(); }}
                        />
                      </FormField>
                      {selectedProvider?.permissions.length ? (
                        <label className="post-create-option">
                          <input
                            type="checkbox"
                            checked={providerPermissionsApproved}
                            onChange={event => { setProviderPermissionsApproved(event.target.checked); markDirty(); }}
                          />
                          <span>
                            <strong>确认 Provider 请求的权限</strong>
                            <small>{selectedProvider.permissions.join("、")}；确认后才会写入本 Revision。</small>
                          </span>
                        </label>
                      ) : null}
                    </div>
                  )}
                  <FormField label="描述" requirement="optional" htmlFor="quickDescription" error={quickForm.formState.errors.description?.message}>
                    <input id="quickDescription" maxLength={1024} placeholder="简要说明这个 Agent 解决什么问题" {...quickForm.register("description", { onChange: markDirty })} />
                  </FormField>
                  <FormField
                    label="Agent 目标与要求"
                    requirement="required"
                    htmlFor="quickPrompt"
                    hint="写清角色、目标、工作边界和回答方式。"
                    error={quickForm.formState.errors.prompt?.message}
                    footer={<div className="field-footer"><span>角色 · 目标 · 边界 · 回答方式</span><span>{prompt.length} / 32768</span></div>}
                  >
                    <textarea id="quickPrompt" rows={7} maxLength={32768} placeholder="例如：你是一名企业技术支持助手。先识别问题类型，再结合知识库给出准确、可执行的处理步骤；信息不足时先提问，不要编造事实。" {...quickForm.register("prompt", { onChange: markDirty })} />
                  </FormField>
                  {template === "research" && (
                    <div className="template-specific">
                      <div className="form-grid two-columns">
                        <FormField label="目标读者" requirement="required" htmlFor="researchAudience" error={quickForm.formState.errors.audience?.message}>
                          <input id="researchAudience" maxLength={256} {...quickForm.register("audience")} />
                        </FormField>
                        <FormField label="输出语言" requirement="required" htmlFor="researchLanguage">
                          <StudioSelect
                            id="researchLanguage"
                            ariaLabel="输出语言"
                            value={language}
                            options={[
                              { value: "zh-CN", label: "简体中文" },
                              { value: "en-US", label: "English" },
                            ]}
                            onValueChange={value => quickForm.setValue("language", value as QuickAgentFormValues["language"], { shouldDirty: true, shouldValidate: true })}
                          />
                        </FormField>
                      </div>
                      <div className="field">
                        <label>调研深度</label>
                        <div className="choice-grid">
                          {[
                            { value: "focused", label: "聚焦", desc: "8 个步骤，适合快速事实核验", time: "约 3 分钟" },
                            { value: "standard", label: "标准", desc: "16 个步骤，兼顾范围和深度", time: "约 8 分钟" },
                            { value: "deep", label: "深度", desc: "28 个步骤，多来源交叉验证", time: "约 15 分钟" },
                          ].map(o => (
                            <button key={o.value} className={`choice-card${depth === o.value ? " selected" : ""}`} type="button" onClick={() => quickForm.setValue("depth", o.value as QuickAgentFormValues["depth"], { shouldDirty: true })}>
                              <span className="choice-check"><Check size={14} /></span>
                              <strong>{o.label}</strong><span>{o.desc}</span><small>{o.time}</small>
                            </button>
                          ))}
                        </div>
                      </div>
                      <FormField label="默认输出" requirement="required" htmlFor="researchFormat">
                        <StudioSelect
                          id="researchFormat"
                          ariaLabel="默认输出"
                          value={format}
                          options={[
                            { value: "report", label: "结构化研究报告" },
                            { value: "brief", label: "决策简报" },
                            { value: "evidence-table", label: "证据矩阵与结论" },
                          ]}
                          onValueChange={value => quickForm.setValue("format", value as QuickAgentFormValues["format"], { shouldDirty: true, shouldValidate: true })}
                        />
                      </FormField>
                    </div>
                  )}
                </section>

                {/* 第 2 步：绑定能力 */}
                <section className={`wizard-panel${step === 2 ? " active" : ""}`} hidden={step !== 2}>
                  <div className="panel-heading">
                    <span className="panel-index">02</span>
                    <div><h2>选择 Agent 可以使用的能力</h2><p>所有依赖都会在构建时锁定版本和摘要，并由权限策略控制调用。</p></div>
                  </div>
                  {usesNativeCodexTools && (
                    <div className="inline-alert warning codex-capability-notice">
                      <CircleAlert size={16} />
                      <div><strong>Codex 使用原生工具，不绑定 KsADK 内置 Tool</strong><p>Codex CLI 自身提供工具能力。MCP（streamable-http）与 Skill 仍可绑定：MCP 经 Codex 配置注入，Skill 以原生 SkillInput 注入。模型仍需选择并配置凭证。</p></div>
                    </div>
                  )}
                  <div className="capability-section">
                    <div className="capability-heading">
                      <span className="capability-icon"><Cpu size={15} /></span>
                      <div>
                        <h3>模型 <span className="studio-field-requirement required" aria-hidden="true">*</span><span className="sr-only">必填</span></h3>
                        <p>至少选择一个；支持多选</p>
                      </div>
                    </div>
                    {selectedModelNeedsCredential && (
                    <div className="model-profile-control">
                      <button
                        className="button secondary"
                        type="button"
                        onClick={() => setConfigModel(selectedModelItems[0] || null)}
                      >
                        配置凭证
                      </button>
                    </div>
                    )}
                    <StudioMultiSelect
                      ariaLabel="选择模型"
                      items={models}
                      selectedIds={selectedModels}
                      getId={item => item.resourceId}
                      getLabel={item => item.displayName}
                      getDescription={item => `${item.contract?.model || item.name} · ${hasConfiguredCredential(item) ? "凭证已配置" : "需配置凭证"}`}
                      onChange={ids => { setSelectedModels(ids); markDirty(); }}
                      searchPlaceholder="搜索模型"
                      emptyMessage="没有可用模型"
                    />
                    <span className="helper">{selectedModelStatus}</span>
                  </div>
                  {!usesNativeCodexTools && (
                    <div className="capability-section">
                      <div className="capability-heading">
                        <span className="capability-icon"><Wrench size={15} /></span>
                        <div><h3>Tool 与权限</h3><p>选择本地 Tool，并设置默认审批级别</p></div>
                      </div>
                      <div className="segmented-control" aria-label="Tool 权限模板">
                        {["loose", "strict", "custom"].map(p => (
                          <button key={p} className={policy === p ? "selected" : ""} type="button" onClick={() => { setPolicy(p); markDirty(); }}>
                            {{ loose: "宽松", strict: "严格", custom: "自定义" }[p]}
                          </button>
                        ))}
                      </div>
                      <p className="policy-description">{policyMeta.description}</p>
                      <StudioMultiSelect
                        ariaLabel="选择 Tool"
                        items={tools}
                        selectedIds={selectedTools}
                        getId={item => item.resourceId}
                        getLabel={item => item.displayName}
                        getDescription={item => `${item.version} · ${item.description || "本地 Tool"}`}
                        onChange={ids => { setSelectedTools(ids); markDirty(); }}
                        searchPlaceholder="搜索 Tool"
                        emptyMessage="没有可用 Tool"
                      />
                    </div>
                  )}
                  <div className="capability-section">
                    <div className="capability-heading">
                      <span className="capability-icon"><Network size={15} /></span>
                      <div><h3>MCP Server</h3><p>连接外部服务并提供可发现的 Tool；codex 经 config_overrides 注入 streamable-http MCP</p></div>
                      <button className="button secondary small" type="button" onClick={() => setShowMcpConnect(true)}>
                        <Plus size={14} /><span>连接 MCP</span>
                      </button>
                    </div>
                    <StudioMultiSelect
                      ariaLabel="选择 MCP Server"
                      items={mcps}
                      selectedIds={selectedMcp}
                      disabledIds={mcps.filter(item => !selectedMcp.includes(item.resourceId) && mcpUnavailableReason(item, runtime)).map(item => item.resourceId)}
                      getId={item => item.resourceId}
                      getLabel={item => item.displayName}
                      getDescription={item => mcpUnavailableReason(item, runtime) || `${item.description || "MCP Server"} · ${item.health?.toolCount || 0} Tool · ${item.status === "ready" ? "Ready" : item.status}`}
                      onChange={ids => { setSelectedMcp(ids); markDirty(); }}
                      searchPlaceholder="搜索 MCP Server"
                      emptyMessage="没有已连接的 MCP Server"
                    />
                  </div>
                  <div className="capability-section">
                    <div className="capability-heading">
                      <span className="capability-icon"><Sparkles size={15} /></span>
                      <div><h3>Skill</h3><p>按需注入可复用的方法、知识和任务约束</p></div>
                    </div>
                    <StudioMultiSelect
                      ariaLabel="选择 Skill"
                      items={skills}
                      selectedIds={selectedSkills}
                      getId={item => item.resourceId}
                      getLabel={item => item.displayName}
                      getDescription={item => `${item.version} · ${item.description || "版本化 Skill"}`}
                      onChange={ids => { setSelectedSkills(ids); markDirty(); }}
                      searchPlaceholder="搜索 Skill"
                      emptyMessage="没有已安装的 Skill"
                    />
                  </div>
                </section>

                {/* 第 3 步：Prompt 与策略 */}
                <section className={`wizard-panel${step === 3 ? " active" : ""}`} hidden={step !== 3}>
                  <div className="panel-heading">
                    <span className="panel-index">03</span>
                    <div><h2>检查系统提示词与任务契约</h2><p>保存前可以继续编辑，创建时会完整写入 Agent Draft。</p></div>
                    <button className={`button secondary small prompt-optimize-button${promptStatus === "composing" ? " is-working" : ""}`} type="button" disabled={promptStatus === "composing"} aria-busy={promptStatus === "composing"} onClick={optimizePromptWithModel}>
                      <RefreshCw size={14} aria-hidden="true" /><span className={promptStatus === "composing" ? "text-shimmer" : undefined}>{promptStatus === "composing" ? (promptOperation === "optimize" ? "正在优化" : "正在生成") : "一键优化 Prompt"}</span>
                    </button>
                  </div>
                  <div className={`prompt-status${promptStatus === "composing" ? " is-working" : ""}`} role="status" aria-live="polite">
                    <span className={`status-dot ${promptStatus === "done" ? "success" : "info"}`} />
                    <span>{promptStatus === "composing" ? (promptOperation === "optimize" ? "正在使用生成模型优化角色与任务契约，通常需要几十秒" : "正在根据模板与能力生成角色与任务契约") : promptStatus === "done" ? "角色与任务契约已根据当前选择生成" : "进入此步骤后生成 Prompt"}</span>
                  </div>
                  <FormField label="角色与系统提示词" requirement="required" htmlFor="composedSystemPrompt" hint="定义角色、目标、工作边界和回答原则" error={quickForm.formState.errors.systemPrompt?.message}>
                    <textarea id="composedSystemPrompt" className="prompt-editor" rows={16} {...quickForm.register("systemPrompt", { onChange: markDirty })} />
                  </FormField>
                  <FormField label="任务契约" requirement="optional" htmlFor="composedTaskPrompt" hint="约束每次请求的执行步骤、工具使用和交付结构" error={quickForm.formState.errors.taskPrompt?.message}>
                    <textarea id="composedTaskPrompt" className="prompt-editor" rows={10} {...quickForm.register("taskPrompt", { onChange: markDirty })} />
                  </FormField>
                  <details className="pcm-policy-card">
                    <summary>
                      <span>
                        <strong>上下文与记忆策略</strong>
                        <small>按 Runtime 能力控制 Prompt 归属、上下文优化和长期记忆</small>
                      </span>
                    </summary>
                    <div className="pcm-policy-body form-grid two-columns">
                      <FormField label="上下文责任边界" requirement="optional" htmlFor="contextOwnership" hint="决定由平台、框架或原生 Runtime 负责最终输入。">
                        <StudioSelect
                          id="contextOwnership"
                          ariaLabel="上下文责任边界"
                          value={contextOwnership}
                          options={contextOwnershipOptions}
                          onValueChange={value => { setContextOwnership(value); markDirty(); }}
                        />
                      </FormField>
                      <FormField label="上下文优化" requirement="optional" htmlFor="contextEngineRollout" hint="控制预算规划、压缩和降载能力的启用阶段。">
                        <StudioSelect
                          id="contextEngineRollout"
                          ariaLabel="上下文优化"
                          value={contextEngineRollout}
                          options={[
                            { value: "off", label: "Runtime 默认", description: "不启用平台上下文优化" },
                            { value: "shadow", label: "仅观察", description: "记录规划证据但不接管输入" },
                            { value: "enabled", label: "正式启用", description: "按预算规划并组装上下文" },
                          ]}
                          onValueChange={value => { setContextEngineRollout(value); markDirty(); }}
                        />
                      </FormField>
                      <label className="post-create-option">
                        <input
                          type="checkbox"
                          checked={memoryEnabled}
                          onChange={event => { setMemoryEnabled(event.target.checked); markDirty(); }}
                        />
                        <span><strong>启用长期记忆</strong><small>按 Agent 和用户作用域召回相关事实；凭证不会写入 Agent 配置。</small></span>
                      </label>
                      <FormField label="记忆写入" requirement="optional" htmlFor="memoryWriteRollout" hint="关闭记忆时固定为不写入。">
                        <StudioSelect
                          id="memoryWriteRollout"
                          ariaLabel="记忆写入"
                          value={memoryEnabled ? memoryWriteRollout : "off"}
                          disabled={!memoryEnabled}
                          options={[
                            { value: "off", label: "不写入" },
                            { value: "shadow", label: "仅生成候选" },
                            { value: "enabled", label: "允许写入" },
                          ]}
                          onValueChange={value => { setMemoryWriteRollout(value); markDirty(); }}
                        />
                      </FormField>
                    </div>
                  </details>
                </section>

                {/* 第 4 步：检查并创建 */}
                <section className={`wizard-panel${step === 4 ? " active" : ""}`} hidden={step !== 4}>
                  <div className="panel-heading">
                    <span className="panel-index">04</span>
                    <div><h2>检查配置并创建</h2><p>确认 Agent 身份、能力依赖和创建后的动作。</p></div>
                  </div>
                  <div className="review-block">
                    <div className="review-title"><span>Agent</span><button className="text-button" type="button" onClick={() => gotoStep(1)}>编辑</button></div>
                    <div className="review-agent">
                      <span className="agent-avatar">{template === "research" ? <Search size={16} /> : <Bot size={16} />}</span>
                      <div><strong>{name}</strong><span>{slug} · {runtime} · {templateLabel}</span><p>{prompt || "等待填写系统提示词"}</p></div>
                    </div>
                  </div>
                  <div className="review-block">
                    <div className="review-title"><span>能力绑定</span><button className="text-button" type="button" onClick={() => gotoStep(2)}>编辑</button></div>
                    <div className="review-capabilities">
                      <div className="review-capability"><Cpu size={16} /><div><strong>{selectedModelItems[0]?.displayName || "模型"}</strong></div></div>
                      <div className="review-capability"><Wrench size={16} /><div><strong>{effectiveSelectedTools.length} 个 Tool</strong><span>{usesNativeCodexTools ? "由 Codex Runtime 提供原生工具" : policyMeta.title}</span></div></div>
                      <div className="review-capability"><Network size={16} /><div><strong>{selectedMcp.length} 个 MCP</strong><span>{selectedMcp.length ? "已连接外部服务" : "未绑定"}</span></div></div>
                      <div className="review-capability"><Sparkles size={16} /><div><strong>{selectedSkills.length} 个 Skill</strong><span>{selectedSkills.length ? "已注入版本化能力" : "未绑定"}</span></div></div>
                    </div>
                  </div>
                  <div className="review-block">
                    <div className="review-title"><span>Prompt</span><button className="text-button" type="button" onClick={() => gotoStep(3)}>编辑</button></div>
                    <div className="prompt-preview">{systemPrompt || prompt || "等待生成"}</div>
                  </div>
                  <label className="post-create-option">
                    <input type="checkbox" {...quickForm.register("buildAfterCreate")} />
                    <span><strong>{isManagedRuntime ? "创建后立即校验 YAML 声明并打开会话" : "创建后立即构建并打开会话"}</strong><small>{isManagedRuntime ? "只冻结 YAML 和 runtime 摘要；部署时不会上传代码包。" : "生成不可变 AgentBundle，完成后进入 Chat 工作台"}</small></span>
                  </label>
                </section>

                {createError && (
                  <div className="inline-alert error wizard-error-summary" role="alert">
                    <CircleAlert size={16} />
                    <div><strong>需要处理一项配置</strong><p>{createError}</p></div>
                  </div>
                )}

                <footer className="wizard-actions">
                  <button className="button secondary" type="button" disabled={step === 1} onClick={() => gotoStep(step - 1)}>
                    <ArrowLeft size={16} /><span>上一步</span>
                  </button>
                  <span className="wizard-progress">第 {step} 步，共 4 步</span>
                  <dl className="summary-chips" aria-label="配置摘要">
                    <div><dt>模板</dt><dd>{templateLabel}</dd></div>
                    <div><dt>Runtime</dt><dd>{runtimeLabel}</dd></div>
                    <div><dt>模型</dt><dd>{reviewModel}</dd></div>
                    <div><dt>Tool</dt><dd>{effectiveSelectedTools.length}</dd></div>
                    <div><dt>MCP</dt><dd>{selectedMcp.length}</dd></div>
                    <div><dt>Skill</dt><dd>{selectedSkills.length}</dd></div>
                  </dl>
                  <button className="button tertiary summary-toggle" type="button" aria-expanded={summaryOpen} onClick={() => setSummaryOpen(v => !v)}>
                    <PanelRight size={16} /><span>完整摘要</span>
                  </button>
                  <div className="wizard-flow-actions">
                    <button className="button secondary" type="button" onClick={saveDraft}>保存草稿</button>
                    {step < 4 ? (
                      <button className="button accent" type="button" onClick={() => gotoStep(step + 1)}>
                        <span>继续</span><ArrowRight size={16} />
                      </button>
                    ) : (
                      <button className="button accent" type="submit" disabled={submitting}>
                        <Plus size={16} /><span>{submitting ? "正在创建" : "创建 Agent"}</span>
                      </button>
                    )}
                  </div>
                </footer>
              </form>
              </FormProvider>

              <StudioDrawer
                open={summaryOpen}
                compact
                title="配置摘要"
                subtitle="检查本轮创建使用的 Runtime、模型、能力与权限策略。"
                onOpenChange={setSummaryOpen}
              >
                <div className="wizard-summary-content">
                <dl>
                  <div><dt>模板</dt><dd>{templateLabel}</dd></div>
                  <div><dt>Runtime</dt><dd>{runtimeLabel}</dd></div>
                  <div><dt>模型</dt><dd>{reviewModel}</dd></div>
                  <div><dt>Skill</dt><dd>{selectedSkills.length}</dd></div>
                  <div><dt>MCP</dt><dd>{selectedMcp.length}</dd></div>
                  <div><dt>Tool</dt><dd>{effectiveSelectedTools.length}</dd></div>
                  <div><dt>策略</dt><dd>{template === "research" ? "Plan-Act-Observe" : "Direct"}</dd></div>
                </dl>
                <div className="summary-divider" />
                <div className="summary-note">
                  <ShieldCheck size={16} />
                  <div><strong>{policyMeta.title}</strong><p>{policyMeta.description}</p></div>
                </div>
                <div className="summary-note">
                  <Package size={16} />
                  <div><strong>{isManagedRuntime ? "声明校验" : "不可变构建"}</strong><p>{isManagedRuntime ? "冻结 YAML 与 runtime 摘要；云端部署不使用代码包。" : usesNativeCodexTools ? "Skill 与 MCP 将锁定版本与摘要；工具由 Codex Runtime 提供。" : "Skill、MCP 和 Tool 将锁定版本与摘要。"}</p></div>
                </div>
                </div>
              </StudioDrawer>
            </div>
          )}
        </div>
      </div>
      {configModel && (
        <ModelCredentialDrawer
          model={configModel as any}
          onClose={() => setConfigModel(null)}
          onChanged={loadCatalog}
        />
      )}
      {showMcpConnect && (
        <McpConnectDrawer
          onClose={() => setShowMcpConnect(false)}
          onConnected={() => {
            setShowMcpConnect(false);
            loadCatalog();
          }}
        />
      )}
    </div>
  );
}

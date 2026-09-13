import { SubAgentBindingsEditor, validateSubAgentBindings, type SubAgentBinding } from "../components/SubAgentBindingsEditor";
import { CodexProviderPermissions, STUDIO_CODEX_PROVIDER_REF } from "../components/CodexProviderPermissions";
import { useEffect, useMemo, useState } from "react";
import { Check, CircleAlert, Package } from "lucide-react";
import { zodResolver } from "@hookform/resolvers/zod";
import { FormProvider, useForm, type Resolver } from "react-hook-form";
import { apiFetch } from "../api";
import { showToast } from "../components/Toast";
import { AgentAppearanceEditor } from "../components/AgentAppearanceEditor";
import { NativePluginBindings, type NativePluginBinding } from "../components/NativePluginBindings";
import { PlatformResourceBindings } from "../components/PlatformResourceBindings";
import type { AgentAppearance } from "../components/AgentAvatar";
import { FormField } from "../components/ui/FormField";
import { StudioMultiSelect } from "../components/ui/StudioMultiSelect";
import { StudioSelect } from "../components/ui/StudioSelect";
import { CodeViewer } from "../components/ui/CodeViewer";
import { applyApiFieldErrors } from "../lib/formErrors";
import { mcpUnavailableReason } from "../lib/mcpCompatibility";
import { agentEditSchema, type AgentEditFormValues } from "../schemas/agentForms";
import {
  parseProviderConfig,
  providerConsentKey,
  providerOptionDescription,
  type AgentProviderCatalogItem,
} from "../agentProviders";

export interface EditorCatalogItem {
  resourceId: string;
  kind: string;
  name: string;
  displayName: string;
  version: string;
  status: string;
  contract?: { name?: string; model?: string; executor?: string; materialization?: string; discoveredTools?: unknown[] };
  health?: { toolCount?: number };
}

interface AgentDetail {
  draft: {
    metadata: { id: string; name: string; revision: number; labels?: Record<string, string>; appearance?: AgentAppearance };
    spec: {
      description?: string;
      runtime?: {
        type?: string;
        projectPath?: string;
        entryPoint?: string;
        agentVariable?: string;
        version?: string;
        detection?: string;
        providerRef?: string;
        providerConfig?: Record<string, unknown>;
      };
      subAgents?: SubAgentBinding[];
      instructions?: { system?: string; task?: string };
      soul?: {
        schemaVersion?: string;
        identity?: string;
        principles?: string[];
        boundaries?: string[];
        tone?: string | null;
      } | null;
      execution?: {
        strategy?: string;
        maxSteps?: number;
        timeoutSeconds?: number;
        sandbox?: string | null;
        approvalMode?: string | null;
        [key: string]: unknown;
      };
      context?: {
        ownership?: string;
        promptOwnership?: string;
        rollout?: { contextEngine?: string; memoryWrite?: string; [key: string]: unknown };
        [key: string]: unknown;
      };
      memory?: {
        enabled?: boolean;
        providerRef?: string;
        recall?: {
          enabled?: boolean;
          maxTokens?: number;
          topK?: number;
          minScore?: number;
          [key: string]: unknown;
        };
        write?: {
          mode?: string;
          flushBeforeCompaction?: boolean;
          [key: string]: unknown;
        };
        [key: string]: unknown;
      };
      bindings?: {
        modelProfileId?: string | null;
        modelProfileIds?: string[];
        skills?: CapabilityBindingValue[];
        mcpServers?: CapabilityBindingValue[];
        tools?: CapabilityBindingValue[];
        plugins?: NativePluginBinding[];
        [key: string]: unknown;
      };
      security?: {
        allowedPermissions?: string[];
        [key: string]: unknown;
      };
      [key: string]: unknown;
    };
  };
  bindingProjection?: {
    unresolvedMcpServers?: Array<{ name: string; reason: string }>;
  };
  soulProjection?: {
    present?: boolean;
    source?: string;
    sourceRevision?: number;
    schemaVersion?: string;
    digest?: string | null;
    digestAlgorithm?: string;
    compileTarget?: string;
    compileOrder?: string;
  };
}

interface CapabilityBindingValue {
  resourceId: string;
  enabled?: boolean;
  approval?: string | null;
  config?: Record<string, unknown>;
  [key: string]: unknown;
}

const TERMINAL_OPERATION_STATES = new Set(["SUCCEEDED", "FAILED", "CANCELLED", "TIMED_OUT"]);

function modelName(item?: EditorCatalogItem) {
  return item?.contract?.model || item?.name || "";
}

function runtimeTitle(runtime: string) {
  if (runtime === "harness") return "KsADK Harness";
  if (runtime === "adk") return "ADKRuntimeAdapter";
  if (runtime === "langgraph") return "LangGraphRuntimeAdapter";
  if (runtime === "plugin") return "External AgentProvider";
  return "CodexRuntimeAdapter";
}

function runtimeManifest(runtime: string) {
  if (runtime === "codex" || runtime === "harness") return { type: runtime };
  return {
    type: runtime,
    projectPath: ".",
    entryPoint: runtime === "adk" ? "agent.py" : "graph.py",
    agentVariable: runtime === "adk" ? "root_agent" : "app",
  };
}

function withHistoricalSelections(
  items: EditorCatalogItem[],
  selectedIds: string[],
  kind: string,
): EditorCatalogItem[] {
  const known = new Set(items.map(item => item.resourceId));
  return [
    ...items,
    ...selectedIds.filter(id => !known.has(id)).map(id => ({
      resourceId: id,
      kind,
      name: id,
      displayName: id,
      version: "历史绑定 · 未进入资源目录",
      status: "unresolved",
      ...(kind === "model" ? { contract: { model: id } } : {}),
    })),
  ];
}

function mergeCapabilityBindings(
  original: CapabilityBindingValue[] | undefined,
  selectedIds: string[],
): CapabilityBindingValue[] {
  const existing = new Map((original || []).map(binding => [binding.resourceId, binding]));
  return selectedIds.map(resourceId => existing.get(resourceId) || { resourceId, enabled: true });
}

function parseLineList(value: string): string[] {
  return value.split(/\r?\n/).map(item => item.trim()).filter(Boolean);
}

async function waitForBuild(operationId: string) {
  for (let attempt = 0; attempt < 1200; attempt += 1) {
    const response = await apiFetch(`/api/v1/operations/${encodeURIComponent(operationId)}`);
    const operation = await response.json().catch(() => null);
    if (!response.ok) throw new Error(operation?.error?.message || `构建状态获取失败（${response.status}）`);
    if (TERMINAL_OPERATION_STATES.has(operation.status)) {
      if (operation.status !== "SUCCEEDED") throw new Error(operation.error?.message || "构建未完成");
      return operation;
    }
    await new Promise(resolve => window.setTimeout(resolve, 200));
  }
  throw new Error("构建等待超时");
}

export function AgentEditor({
  agentId,
  catalog,
  providers = [],
  activeSection = 1,
  onSaved,
  onAppearanceSaved,
  onCancel,
}: {
  agentId: string;
  catalog: EditorCatalogItem[];
  providers?: AgentProviderCatalogItem[];
  activeSection?: number;
  onSaved: (agentId: string, openChat: boolean) => void;
  onAppearanceSaved?: () => void;
  onCancel?: () => void;
}) {
  const [detail, setDetail] = useState<AgentDetail | null>(null);
  const [harnessPermission, setHarnessPermission] = useState(false);
  const [harnessPermissionTouched, setHarnessPermissionTouched] = useState(false);
  const [loadError, setLoadError] = useState("");
  const agentForm = useForm<AgentEditFormValues>({
    resolver: zodResolver(agentEditSchema) as Resolver<AgentEditFormValues>,
    defaultValues: {
      name: "",
      slug: agentId,
      runtimeType: "codex",
      prompt: "",
      description: "",
    },
  });
  const resetAgentForm = agentForm.reset;
  const { name, slug, runtimeType: runtime, prompt } = agentForm.watch();
  const [defaultModel, setDefaultModel] = useState("");
  const [selectedModels, setSelectedModels] = useState<string[]>([]);
  const [selectedSkills, setSelectedSkills] = useState<string[]>([]);
  const [selectedMcp, setSelectedMcp] = useState<string[]>([]);
  const [selectedTools, setSelectedTools] = useState<string[]>([]);
  const [selectedPlugins, setSelectedPlugins] = useState<NativePluginBinding[]>([]);
  const [pluginsPending, setPluginsPending] = useState(false);
  const [visibleSection, setVisibleSection] = useState(activeSection);
  const [runtimeProjectPath, setRuntimeProjectPath] = useState(".");
  const [runtimeEntryPoint, setRuntimeEntryPoint] = useState("");
  const [runtimeAgentVariable, setRuntimeAgentVariable] = useState("root_agent");
  const [providerRef, setProviderRef] = useState("");
  const [subAgents, setSubAgents] = useState<SubAgentBinding[]>([]);
  const [subAgentsTouched, setSubAgentsTouched] = useState(false);
  const [providerConfigText, setProviderConfigText] = useState("{}");
  const [providerConsent, setProviderConsent] = useState<{ key: string; approved: boolean } | null>(null);
  const [executionStrategy, setExecutionStrategy] = useState("direct");
  const [executionMaxSteps, setExecutionMaxSteps] = useState(12);
  const [executionTimeoutSeconds, setExecutionTimeoutSeconds] = useState(120);
  const [contextOwnership, setContextOwnership] = useState("auto");
  const [contextEngineRollout, setContextEngineRollout] = useState("shadow");
  const [soulEnabled, setSoulEnabled] = useState(false);
  const [soulIdentity, setSoulIdentity] = useState("");
  const [soulPrinciples, setSoulPrinciples] = useState("");
  const [soulBoundaries, setSoulBoundaries] = useState("");
  const [soulTone, setSoulTone] = useState("");
  const [soulTouched, setSoulTouched] = useState(false);
  const [memoryEnabled, setMemoryEnabled] = useState(false);
  const [memoryProviderRef, setMemoryProviderRef] = useState("local-default");
  const [memoryRecallEnabled, setMemoryRecallEnabled] = useState(true);
  const [memoryRecallMaxTokens, setMemoryRecallMaxTokens] = useState(1600);
  const [memoryRecallTopK, setMemoryRecallTopK] = useState(8);
  const [memoryRecallMinScore, setMemoryRecallMinScore] = useState(0.45);
  const [memoryWriteMode, setMemoryWriteMode] = useState("candidate");
  const [memoryFlushBeforeCompaction, setMemoryFlushBeforeCompaction] = useState(true);
  const [memoryWriteRollout, setMemoryWriteRollout] = useState("shadow");
  const [memoryTouched, setMemoryTouched] = useState(false);
  const [buildAfterSave, setBuildAfterSave] = useState(true);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState("");

  const models = useMemo(() => catalog.filter(item => item.kind === "model" && ["ready", "missing-secret"].includes(item.status)), [catalog]);
  const skills = useMemo(() => catalog.filter(item => item.kind === "skill" && item.status === "ready"), [catalog]);
  const mcps = useMemo(() => catalog.filter(item => item.kind === "mcp"), [catalog]);
  const tools = useMemo(() => catalog.filter(item => (
    item.kind === "tool"
    && item.status === "ready"
    && ["builtin", "python"].includes(item.contract?.executor || "builtin")
  )), [catalog]);
  const visibleModels = useMemo(() => withHistoricalSelections(models, selectedModels, "model"), [models, selectedModels]);
  const visibleSkills = useMemo(() => withHistoricalSelections(skills, selectedSkills, "skill"), [skills, selectedSkills]);
  const visibleMcps = useMemo(() => withHistoricalSelections(mcps, selectedMcp, "mcp"), [mcps, selectedMcp]);
  const visibleTools = useMemo(() => withHistoricalSelections(tools, selectedTools, "tool"), [tools, selectedTools]);
  const selectedModelItems = selectedModels.map(id => visibleModels.find(item => item.resourceId === id)).filter(Boolean) as EditorCatalogItem[];
  const visibleProviders = useMemo<AgentProviderCatalogItem[]>(() => {
    if (!providerRef || providers.some(item => item.providerRef === providerRef)) return providers;
    return [{
      providerRef,
      pluginId: providerRef,
      resolvedVersion: "历史版本",
      displayName: providerRef,
      state: "disabled",
      compatible: false,
      selectable: false,
      reason: { code: "AGENT_PROVIDER_NOT_INSTALLED", message: "该历史 Provider 当前未安装" },
      permissions: [],
      isolation: "process",
      configSchemaDeclared: false,
      secretFields: [],
    }, ...providers];
  }, [providerRef, providers]);
  const selectedProvider = visibleProviders.find(item => item.providerRef === providerRef);
  const codexProvider = providers.find(item => item.providerRef === STUDIO_CODEX_PROVIDER_REF);
  const permissionProvider = runtime === "codex" ? codexProvider : runtime === "plugin" ? selectedProvider : undefined;
  const consentKey = JSON.stringify([agentId, runtime, providerConsentKey(permissionProvider)]);
  const savedProviderRef = detail?.draft.spec.runtime?.type === "codex"
    ? STUDIO_CODEX_PROVIDER_REF : detail?.draft.spec.runtime?.providerRef;
  const savedPermissions = new Set(detail?.draft.spec.security?.allowedPermissions || []);
  const providerPermissionsApproved = providerConsent?.key === consentKey
    ? providerConsent.approved
    : Boolean(detail?.draft.metadata.id === agentId && permissionProvider
      && savedProviderRef === permissionProvider.providerRef
      && permissionProvider.permissions.every(permission => savedPermissions.has(permission)));
  const setProviderPermissionsApproved = (approved: boolean) => setProviderConsent({ key: consentKey, approved });
  const providerOptions = visibleProviders.map(item => ({
    value: item.providerRef,
    label: item.displayName,
    description: providerOptionDescription(item),
    disabled: !item.selectable,
  }));

  useEffect(() => {
    let active = true;
    setDetail(null);
    setLoadError("");
    apiFetch(`/api/v1/agents/${encodeURIComponent(agentId)}`)
      .then(async response => {
        const payload = await response.json().catch(() => null);
        if (!response.ok) throw new Error(payload?.error?.message || `Agent 加载失败（${response.status}）`);
        if (!active) return;
        const draft = payload.draft;
        const bindings = draft.spec?.bindings || {};
        const ids = bindings.modelProfileIds?.length
          ? bindings.modelProfileIds
          : bindings.modelProfileId ? [bindings.modelProfileId] : [];
        setDetail(payload);
        setHarnessPermission(Boolean(payload.draft?.spec?.security?.allowedPermissions?.includes("process:host-user")));
        setHarnessPermissionTouched(false);
        resetAgentForm({
          name: draft.metadata.name || "",
          slug: draft.metadata.id || agentId,
          runtimeType: draft.spec?.runtime?.type || draft.metadata.labels?.["agentkit.ksyun.com/framework"] || "codex",
          prompt: draft.spec?.instructions?.system || "",
          description: draft.spec?.description || "",
        });
        setSelectedModels(ids);
        setDefaultModel(bindings.modelProfileId || ids[0] || "");
        setSelectedSkills((bindings.skills || []).map((item: { resourceId: string }) => item.resourceId));
        setSelectedMcp((bindings.mcpServers || []).map((item: { resourceId: string }) => item.resourceId));
        setSelectedTools((bindings.tools || []).map((item: { resourceId: string }) => item.resourceId));
        setSelectedPlugins(bindings.plugins || []);
        setRuntimeProjectPath(String(draft.spec?.runtime?.projectPath || "."));
        setRuntimeEntryPoint(String(draft.spec?.runtime?.entryPoint || (draft.spec?.runtime?.type === "langgraph" ? "graph.py" : "agent.py")));
        setRuntimeAgentVariable(String(draft.spec?.runtime?.agentVariable || (draft.spec?.runtime?.type === "langgraph" ? "app" : "root_agent")));
        setProviderRef(String(draft.spec?.runtime?.providerRef || ""));
        setProviderConfigText(JSON.stringify(draft.spec?.runtime?.providerConfig || {}, null, 2));
        setExecutionStrategy(String(draft.spec?.execution?.strategy || "direct"));
        setExecutionMaxSteps(Number(draft.spec?.execution?.maxSteps ?? 12));
        setExecutionTimeoutSeconds(Number(draft.spec?.execution?.timeoutSeconds ?? 120));
        setContextOwnership(String(draft.spec?.context?.ownership || "auto"));
        setContextEngineRollout(String(draft.spec?.context?.rollout?.contextEngine || "shadow"));
        setSoulEnabled(Boolean(draft.spec?.soul));
        setSoulIdentity(String(draft.spec?.soul?.identity || ""));
        setSoulPrinciples((draft.spec?.soul?.principles || []).join("\n"));
        setSoulBoundaries((draft.spec?.soul?.boundaries || []).join("\n"));
        setSoulTone(String(draft.spec?.soul?.tone || ""));
        setSoulTouched(false);
        setMemoryEnabled(Boolean(draft.spec?.memory?.enabled));
        setMemoryProviderRef(String(draft.spec?.memory?.providerRef || "local-default"));
        setMemoryRecallEnabled(draft.spec?.memory?.recall?.enabled ?? true);
        setMemoryRecallMaxTokens(Number(draft.spec?.memory?.recall?.maxTokens ?? 1600));
        setMemoryRecallTopK(Number(draft.spec?.memory?.recall?.topK ?? 8));
        setMemoryRecallMinScore(Number(draft.spec?.memory?.recall?.minScore ?? 0.45));
        setMemoryWriteMode(String(draft.spec?.memory?.write?.mode || "candidate"));
        setMemoryFlushBeforeCompaction(draft.spec?.memory?.write?.flushBeforeCompaction ?? true);
        setMemoryWriteRollout(String(draft.spec?.context?.rollout?.memoryWrite || "shadow"));
        setMemoryTouched(false);
      })
      .catch(error => { if (active) setLoadError(error.message || "Agent 加载失败"); });
    return () => { active = false; };
  }, [agentId, resetAgentForm]);

  useEffect(() => setVisibleSection(activeSection), [activeSection]);

  useEffect(() => {
    if (!detail || selectedModels.length || !models.length) return;
    const currentName = detail.draft.metadata.labels?.["agentkit.ksyun.com/model"];
    const inferred = models.find(item => modelName(item) === currentName)?.resourceId;
    if (inferred) {
      setSelectedModels([inferred]);
      setDefaultModel(inferred);
    }
  }, [detail, models, selectedModels.length]);

  function changeModels(next: string[]) {
    setSelectedModels(next);
    if (!next.includes(defaultModel)) setDefaultModel(next[0] || "");
  }

  const primaryModel = models.find(item => item.resourceId === defaultModel)
    || selectedModelItems[0];
  const supportsMcpEditing = ["harness", "codex", "plugin"].includes(runtime);
  const contextOwnershipOptions = runtime === "harness"
    ? [
      { value: "auto", label: "自动（推荐）", description: "按 Harness 能力选择安全模式" },
      { value: "ksadk", label: "KsADK 管理", description: "统一规划、压缩和保护上下文" },
    ]
    : runtime === "codex"
    ? [
      { value: "auto", label: "自动（推荐）", description: "按 Codex Runtime 能力选择安全投影方式" },
      { value: "native", label: "原生 Runtime 管理", description: "由 Codex 管理最终模型上下文" },
    ]
    : runtime === "langgraph"
      ? [
        { value: "auto", label: "自动（推荐）", description: "按 Runtime 能力选择安全模式" },
        { value: "framework", label: "框架管理", description: "保留 LangGraph 原有上下文行为" },
        { value: "ksadk", label: "KsADK 管理", description: "统一规划、压缩和投影上下文" },
      ]
      : [
        { value: "auto", label: "自动（推荐）", description: "按 Runtime 能力选择安全模式" },
        { value: "framework", label: "框架管理", description: "保留 ADK 原有上下文行为" },
      ];
  const fallbackModel = detail?.draft.metadata.labels?.["agentkit.ksyun.com/model"] || "glm-5.1";
  const preservesManifestModel = runtime === "codex" && selectedModels.length === 0 && Boolean(fallbackModel);
  const isManagedDeclaration = detail?.draft.metadata.labels?.["agentkit.ksyun.com/artifact-type"] === "ManagedRuntime"
    || runtime === "codex";
  const manifestModels = selectedModelItems.map(modelName).filter(Boolean);
  const soulYamlLines = soulEnabled ? [
    "  soul:",
    "    schemaVersion: agentkit.soul/v1",
    "    identity: |-",
    ...soulIdentity.split("\n").map(line => `      ${line}`),
    ...(parseLineList(soulPrinciples).length ? [
      "    principles:",
      ...parseLineList(soulPrinciples).map(item => `      - ${item}`),
    ] : []),
    ...(parseLineList(soulBoundaries).length ? [
      "    boundaries:",
      ...parseLineList(soulBoundaries).map(item => `      - ${item}`),
    ] : []),
    ...(soulTone.trim() ? ["    tone: |-", ...soulTone.split("\n").map(line => `      ${line}`)] : []),
  ] : [];
  const codexSoulYamlLines = soulEnabled ? [
    "soul:",
    "  schemaVersion: agentkit.soul/v1",
    "  identity: |-",
    ...soulIdentity.split("\n").map(line => `    ${line}`),
    ...(parseLineList(soulPrinciples).length ? [
      "  principles:",
      ...parseLineList(soulPrinciples).map(item => `    - ${item}`),
    ] : []),
    ...(parseLineList(soulBoundaries).length ? [
      "  boundaries:",
      ...parseLineList(soulBoundaries).map(item => `    - ${item}`),
    ] : []),
    ...(soulTone.trim() ? ["  tone: |-", ...soulTone.split("\n").map(line => `    ${line}`)] : []),
    "soul_source: AgentSpec.soul",
    `soul_digest: ${soulTouched ? "<保存后重新计算>" : detail?.soulProjection?.digest || "<保存后计算>"}`,
  ] : [];
  const soulCompileTarget = detail?.soulProjection?.compileTarget
    || (runtime === "codex"
      ? "managed-runtime.base_instructions"
      : runtime === "plugin"
        ? "instructions/soul.md"
        : "resolved-agent-spec.instructions.system");
  const manifest = runtime === "codex" ? [
    `name: ${slug}`,
    "version: 1.0.0",
    "framework: codex",
    "artifact_type: ManagedRuntime",
    "runtime:",
    "  name: codex",
    ...(detail?.draft.spec.runtime?.version
      ? [`  version: ${detail.draft.spec.runtime.version}`]
      : []),
    `model: ${modelName(primaryModel) || fallbackModel}`,
    ...(manifestModels.length > 1 ? ["models:", ...manifestModels.map(item => `  - ${item}`)] : []),
    ...codexSoulYamlLines,
    "prompt: |-",
    ...prompt.split("\n").map(line => `  ${line}`),
  ].join("\n") : runtime === "plugin" ? [
    "apiVersion: agentkit.ksyun.com/v1alpha1",
    "kind: Agent",
    "metadata:",
    `  id: ${slug}`,
    "spec:",
    "  runtime:",
    "    type: plugin",
    `    providerRef: ${providerRef || "<未选择>"}`,
    "    providerConfig: # Secret 仅保存引用",
    ...providerConfigText.split("\n").map(line => `      ${line}`),
    ...soulYamlLines,
    "  instructions:",
    "    system: |-",
    ...prompt.split("\n").map(line => `      ${line}`),
  ].join("\n") : [
    "apiVersion: agentkit.ksyun.com/v1alpha1",
    "kind: Agent",
    "metadata:",
    `  id: ${slug}`,
    "spec:",
    "  runtime:",
    `    type: ${runtime}`,
    ...(runtime === "harness" ? [] : [
      `    projectPath: ${runtimeProjectPath || "."}`,
      `    entryPoint: ${runtimeEntryPoint || (runtime === "adk" ? "agent.py" : "graph.py")}`,
      `    agentVariable: ${runtimeAgentVariable || (runtime === "adk" ? "root_agent" : "app")}`,
    ]),
    ...soulYamlLines,
    "  instructions:",
    "    system: |-",
    ...prompt.split("\n").map(line => `      ${line}`),
  ].join("\n");

  async function save(values: AgentEditFormValues) {
    if (!detail || saving || pluginsPending) return;
    const resolvedDefaultModel = defaultModel || selectedModels[0] || "";
    if (!resolvedDefaultModel && !preservesManifestModel) {
      setSaveError("请至少绑定一个模型并设置为默认模型");
      return;
    }
    if (soulEnabled && !soulIdentity.trim()) {
      setSaveError("启用 Soul 后必须填写身份定义");
      setVisibleSection(1);
      return;
    }
    if (["adk", "langgraph"].includes(values.runtimeType) && (!runtimeProjectPath.trim() || !runtimeEntryPoint.trim() || !runtimeAgentVariable.trim())) {
      setSaveError("请完整填写项目相对路径、入口文件和 Agent 变量");
      return;
    }
    if (values.runtimeType === "codex" && permissionProvider?.permissions.length && !providerPermissionsApproved) {
      setSaveError("请先确认 Codex Provider 请求的 Agent 权限");
      setVisibleSection(1);
      return;
    }
    let providerConfig: Record<string, unknown> = {};
    if (values.runtimeType === "plugin") {
      if (!selectedProvider?.selectable) {
        setSaveError(selectedProvider?.reason?.message || "所选 AgentProvider 当前不可用");
        return;
      }
      try {
        providerConfig = parseProviderConfig(
          providerConfigText,
          selectedProvider.secretFields,
        );
      } catch (error: any) {
        setSaveError(error.message || "Provider 配置无效");
        return;
      }
      if (selectedProvider.permissions.length && !providerPermissionsApproved) {
        setSaveError("请先确认 AgentProvider 请求的权限");
        return;
      }
    }
    if (!Number.isInteger(executionMaxSteps) || executionMaxSteps < 1 || executionMaxSteps > 100) {
      setSaveError("最大步骤数必须是 1 到 100 的整数");
      return;
    }
    if (!Number.isInteger(executionTimeoutSeconds) || executionTimeoutSeconds < 1 || executionTimeoutSeconds > 3600) {
      setSaveError("超时秒数必须是 1 到 3600 的整数");
      return;
    }
    if (
      memoryTouched
      && (
        !memoryProviderRef.trim()
        || !Number.isInteger(memoryRecallMaxTokens)
        || memoryRecallMaxTokens < 0
        || !Number.isInteger(memoryRecallTopK)
        || memoryRecallTopK < 1
        || memoryRecallTopK > 64
        || !Number.isFinite(memoryRecallMinScore)
        || memoryRecallMinScore < 0
        || memoryRecallMinScore > 1
      )
    ) {
      setSaveError("请检查 Memory Provider 与召回策略范围");
      setVisibleSection(3);
      return;
    }
    const subAgentError = validateSubAgentBindings(subAgents, values.runtimeType);
    if (subAgentError) { setSaveError(subAgentError); setVisibleSection(2); return; }
    setSaving(true);
    setSaveError("");
    let updateSaved = false;
    try {
      const original = detail.draft.spec;
      const spec = JSON.parse(JSON.stringify(original));
      if (values.runtimeType === "harness" && harnessPermissionTouched) {
        const retained = (spec.security?.allowedPermissions || []).filter((p: string) => p !== "process:host-user");
        spec.security = { ...spec.security, allowedPermissions: harnessPermission
          ? [...retained, "process:host-user"] : retained };
      }
      spec.runtime = values.runtimeType === "plugin" ? {
        type: "plugin",
        providerRef: selectedProvider?.providerRef,
        providerConfig,
      } : {
        ...runtimeManifest(values.runtimeType),
        ...(original.runtime || {}),
        type: values.runtimeType,
        ...(["adk", "langgraph"].includes(values.runtimeType) ? {
          projectPath: runtimeProjectPath.trim(),
          entryPoint: runtimeEntryPoint.trim(),
          agentVariable: runtimeAgentVariable.trim(),
        } : {}),
      };
      if (permissionProvider) {
        spec.security = {
          ...(original.security || {}),
          allowedPermissions: [...new Set([
            ...(original.security?.allowedPermissions || []),
            ...permissionProvider.permissions,
          ])].sort(),
        };
      }
      spec.instructions = {
        ...(original.instructions || {}),
        system: values.prompt.trim(),
        task: original.instructions?.task || "",
      };
      if (soulTouched) {
        spec.soul = soulEnabled ? {
          schemaVersion: "agentkit.soul/v1",
          identity: soulIdentity.trim(),
          principles: parseLineList(soulPrinciples),
          boundaries: parseLineList(soulBoundaries),
          tone: soulTone.trim() || null,
        } : null;
      }
      spec.execution = {
        ...(original.execution || {}),
        strategy: executionStrategy,
        maxSteps: executionMaxSteps,
        timeoutSeconds: executionTimeoutSeconds,
      };
      spec.bindings = {
        ...(original.bindings || {}),
        modelProfileId: resolvedDefaultModel || null,
        modelProfileIds: selectedModels,
        skills: mergeCapabilityBindings(original.bindings?.skills, selectedSkills),
        mcpServers: mergeCapabilityBindings(original.bindings?.mcpServers, selectedMcp),
        tools: mergeCapabilityBindings(original.bindings?.tools, selectedTools),
        plugins: selectedPlugins,
      };
      spec.context = {
        ...(original.context || {}),
        ownership: contextOwnership,
        promptOwnership: contextOwnership === "ksadk"
          ? "ksadk"
          : contextOwnership === "framework"
            ? "framework"
            : original.context?.promptOwnership || "framework",
        rollout: {
          ...(original.context?.rollout || {}),
          contextEngine: contextEngineRollout,
          ...(memoryTouched ? { memoryWrite: memoryWriteRollout } : {}),
        },
      };
      if (memoryTouched) {
        const platformMemoryBindingId = String((
          selectedPlugins.find(binding =>
            (binding.config as any)?.binding?.resource?.kind === "memory-instance"
          )?.config as any
        )?.binding?.id || "");
        spec.memory = {
          ...(original.memory || {}),
          enabled: platformMemoryBindingId ? true : memoryEnabled,
          providerRef: platformMemoryBindingId
            ? `binding://${platformMemoryBindingId}`
            : memoryProviderRef.trim(),
          ...(platformMemoryBindingId ? { scopes: ["user"] } : {}),
          recall: {
            ...(original.memory?.recall || {}),
            enabled: platformMemoryBindingId ? true : memoryRecallEnabled,
            maxTokens: memoryRecallMaxTokens,
            topK: memoryRecallTopK,
            minScore: platformMemoryBindingId ? 0 : memoryRecallMinScore,
          },
          write: {
            ...(original.memory?.write || {}),
            mode: memoryWriteMode,
            flushBeforeCompaction: memoryFlushBeforeCompaction,
          },
        };
      }
      const response = await apiFetch(
        `/api/v1/agents/${encodeURIComponent(agentId)}?name=${encodeURIComponent(values.name.trim())}`,
        {
        method: "PUT",
        headers: {
          "Content-Type": "application/json",
          "If-Match": String(detail.draft.metadata.revision),
        },
        body: JSON.stringify(spec),
        },
      );
      const saved = await response.json().catch(() => null);
      if (!response.ok) {
        applyApiFieldErrors(saved, agentForm.setError);
        throw new Error(saved?.error?.message || `保存失败（${response.status}）`);
      }
      updateSaved = true;
      if (saved?.metadata) {
        setDetail(current => current ? { ...current, draft: { metadata: saved.metadata, spec } } : current);
      }
      const savedId = saved?.metadata?.id || agentId;
      showToast(
        "Agent 已更新",
        isManagedDeclaration
          ? "本地配置已保存；更新云端后生效。"
          : "本地声明已保存；已部署版本不会静默改变。",
      );

      if (buildAfterSave) {
        // The PUT response carries the authoritative post-update revision.
        // Falling back to "oldRevision + 1" is unsafe when the server's actual
        // revision differs (e.g. a no-op update that didn't bump, or a stale
        // detail cache).  Re-read the agent to get the ground truth.
        let revision = saved?.metadata?.revision;
        if (!revision || typeof revision !== "number") {
          const refreshed = await apiFetch(`/api/v1/agents/${encodeURIComponent(savedId)}`);
          if (refreshed.ok) {
            const fresh = await refreshed.json().catch(() => null);
            revision = fresh?.draft?.metadata?.revision || detail.draft.metadata.revision;
          } else {
            revision = detail.draft.metadata.revision;
          }
        }
        const buildResponse = await apiFetch(`/api/v1/agents/${encodeURIComponent(savedId)}/builds`, {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "Idempotency-Key": `build-${savedId}-r${revision}-${Date.now()}`,
          },
          body: JSON.stringify({ revision, runEvaluation: false }),
        });
        const operation = await buildResponse.json().catch(() => null);
        if (!buildResponse.ok) throw new Error(operation?.error?.message || `构建提交失败（${buildResponse.status}）`);
        await waitForBuild(operation.id);
        showToast(
          values.runtimeType === "codex" ? "YAML 声明已校验" : `${values.runtimeType} Bundle 构建完成`,
          savedId,
        );
      }
      onSaved(savedId, buildAfterSave);
    } catch (error: any) {
      const disconnected = error instanceof TypeError && /fetch|network|load failed/i.test(error.message);
      const reason = disconnected
        ? "与 Studio 的连接中断，请确认本地服务仍在运行。当前填写的内容已保留。"
        : error.message || "保存失败";
      const message = updateSaved
        ? `配置已保存，但后续构建未完成。${reason}`
        : disconnected ? `尚未确认保存结果。${reason}` : reason;
      setSaveError(message);
      showToast(updateSaved ? "构建未完成" : "保存未完成", message, "error");
    } finally {
      setSaving(false);
    }
  }

  async function saveAppearance(appearance: Required<AgentAppearance>) {
    if (!detail) return;
    const response = await apiFetch(`/api/v1/agents/${encodeURIComponent(agentId)}/appearance`, {
      method: "PUT",
      headers: {
        "Content-Type": "application/json",
        "If-Match": String(detail.draft.metadata.revision),
      },
      body: JSON.stringify(appearance),
    });
    const saved = await response.json().catch(() => null);
    if (!response.ok) throw new Error(saved?.error?.message || `外观保存失败（${response.status}）`);
    setDetail(current => current ? {
      ...current,
      draft: { ...current.draft, metadata: saved.metadata },
    } : current);
    onAppearanceSaved?.();
    showToast("Agent 外观已更新", "列表、会话和 Trace 将使用新的头像。");
  }

  if (loadError) {
    return <div className="inline-alert error"><CircleAlert size={16} /><div><strong>Agent 加载失败</strong><p>{loadError}</p></div></div>;
  }
  if (!detail) return <div className="quick-create"><p>正在加载 Agent 配置…</p></div>;

  return (
    <div className="quick-create">
      <FormProvider {...agentForm}>
      <form
        className="quick-create-form"
        onSubmit={agentForm.handleSubmit(save, errors => {
          const firstError = Object.values(errors).find(error => typeof error?.message === "string");
          setSaveError(String(firstError?.message || "请检查必填配置后重试"));
        })}
        noValidate
      >
        <div className="quick-create-heading">
          <h2 title={slug}>{name || detail.draft.metadata.name}</h2>
          <p>{runtimeTitle(runtime)} · 修改基础信息、模型和运行设置</p>
        </div>
        <nav className="agent-edit-nav" aria-label="Agent 编辑分区">
          {[
            { id: 1, label: "基础与 Prompt" },
            { id: 2, label: "能力绑定" },
            { id: 3, label: "运行策略" },
          ].map(section => (
            <button
              key={section.id}
              type="button"
              className={visibleSection === section.id ? "active" : ""}
              aria-current={visibleSection === section.id ? "page" : undefined}
              onClick={() => setVisibleSection(section.id)}
            >{section.label}</button>
          ))}
        </nav>
        <p className="agent-version-note">保存修改后在本地生效；已部署到云端的版本需重新部署。</p>
        <section className="agent-edit-section" hidden={visibleSection !== 1} aria-label="基础与 Prompt">
        <div className="agent-edit-section-heading">
          <span className="eyebrow">01</span>
          <div><h3>基础与 Prompt</h3></div>
        </div>
        <details className="secondary-settings agent-appearance-disclosure">
          <summary>头像与配色</summary>
        <AgentAppearanceEditor
          name={name || detail.draft.metadata.name}
          appearance={detail.draft.metadata.appearance}
          disabled={saving}
          onSave={saveAppearance}
        />
        </details>
        <div className="form-grid two-columns">
          <FormField label="显示名称" requirement="required" htmlFor="editAgentName" error={agentForm.formState.errors.name?.message}>
            <input id="editAgentName" readOnly {...agentForm.register("name")} />
          </FormField>
          <FormField
            label="本地标识（Slug）"
            requirement="generated"
            htmlFor="editAgentSlug"
            hint="本地唯一标识由创建流程生成；云端 AgentId 由部署服务另行映射。"
            error={agentForm.formState.errors.slug?.message}
          >
            <input id="editAgentSlug" className="mono generated-value" readOnly {...agentForm.register("slug")} />
          </FormField>
        </div>
        <FormField label="Runtime" requirement="required" htmlFor="editAgentRuntime" hint="已有 Build 后不能直接切换 Runtime；需创建迁移 Revision。" error={agentForm.formState.errors.runtimeType?.message}>
          <StudioSelect
            id="editAgentRuntime"
            ariaLabel="Runtime"
            disabled
            value={runtime}
            options={[
              { value: "harness", label: "KsADK Harness" },
              { value: "codex", label: "CodexRuntimeAdapter" },
              { value: "adk", label: "ADKRuntimeAdapter" },
              { value: "langgraph", label: "LangGraphRuntimeAdapter" },
              { value: "plugin", label: "External AgentProvider" },
            ]}
            onValueChange={() => undefined}
          />
        </FormField>
        {runtime === "plugin" && (
          <div className="template-specific" data-testid="edit-external-provider-config">
            <FormField
              label="AgentProvider"
              requirement="required"
              htmlFor="editAgentProvider"
              hint="可以切换到另一个已安装、已启用且兼容的精确版本；Runtime 类型保持不变。"
            >
              <StudioSelect
                id="editAgentProvider"
                ariaLabel="AgentProvider"
                value={providerRef}
                placeholder="Provider 不可用"
                options={providerOptions}
                onValueChange={value => {
                  setProviderRef(value);
                }}
              />
            </FormField>
            {!selectedProvider?.selectable && (
              <div className="inline-alert warning" role="status">
                <CircleAlert size={16} />
                <div>
                  <strong>AgentProvider 当前不可用</strong>
                  <p>{selectedProvider?.reason?.message || "该 Provider 未安装或未启用，请先在插件中心处理。"}</p>
                </div>
              </div>
            )}
            <FormField
              label="Provider 配置"
              requirement="optional"
              htmlFor="editProviderConfig"
              hint="填写 JSON 对象；敏感字段只能保存 Secret 引用，不能保存明文。"
            >
              <textarea
                id="editProviderConfig"
                className="mono"
                rows={6}
                value={providerConfigText}
                onChange={event => setProviderConfigText(event.target.value)}
              />
            </FormField>
            {selectedProvider?.permissions.length ? (
              <label className="post-create-option">
                <input
                  type="checkbox"
                  checked={providerPermissionsApproved}
                  onChange={event => setProviderPermissionsApproved(event.target.checked)}
                />
                <span>
                  <strong>确认 Provider 请求的权限</strong>
                  <small>{selectedProvider.permissions.join("、")}；确认后才会写入新 Revision。</small>
                </span>
              </label>
            ) : null}
          </div>
        )}
        {runtime === "codex" && (
          <CodexProviderPermissions provider={codexProvider} approved={providerPermissionsApproved}
            onChange={setProviderPermissionsApproved} />
        )}
        {runtime === "harness" && <details className="template-specific">
          <summary>本地运行：{harnessPermission ? "已授权" : "未授权"} · 高级权限</summary>
          <label className="post-create-option">
          <input type="checkbox" checked={harnessPermission} onChange={event => {
            setHarnessPermission(event.target.checked); setHarnessPermissionTouched(true);
          }} />
          <span><strong>允许 KsADK Harness 在本机运行</strong>
            <small>仅授权本地执行引擎启动；工具仍受权限与审批策略约束。撤销后保存到新版本，该版本将无法使用本地 Harness。</small>
          </span>
        </label></details>}
        <fieldset className="agent-policy-editor soul-editor" aria-describedby="soulPolicyHint">
          <legend>Soul · 稳定人格</legend>
          <label className="pcm-memory-toggle soul-enable-toggle">
            <input
              type="checkbox"
              checked={soulEnabled}
              onChange={event => {
                setSoulEnabled(event.target.checked);
                setSoulTouched(true);
              }}
            />
            <span>
              <strong>{soulEnabled ? "已声明 Soul" : "未声明 Soul"}</strong>
              <small id="soulPolicyHint">Soul 属于人工审核的 Revision 源，不会从会话或 Memory 自动改写。</small>
            </span>
          </label>
          {soulEnabled ? (
            <div className="agent-policy-fields">
              <FormField label="身份定义" requirement="required" htmlFor="editSoulIdentity" hint="说明 Agent 是谁、承担什么稳定职责。">
                <textarea
                  id="editSoulIdentity"
                  rows={4}
                  maxLength={4096}
                  value={soulIdentity}
                  onChange={event => { setSoulIdentity(event.target.value); setSoulTouched(true); }}
                />
              </FormField>
              <div className="form-grid two-columns soul-list-grid">
                <FormField label="原则" requirement="optional" htmlFor="editSoulPrinciples" hint="每行一条，最多 64 条。">
                  <textarea
                    id="editSoulPrinciples"
                    rows={5}
                    value={soulPrinciples}
                    onChange={event => { setSoulPrinciples(event.target.value); setSoulTouched(true); }}
                  />
                </FormField>
                <FormField label="边界" requirement="optional" htmlFor="editSoulBoundaries" hint="每行一条不可突破的行为边界。">
                  <textarea
                    id="editSoulBoundaries"
                    rows={5}
                    value={soulBoundaries}
                    onChange={event => { setSoulBoundaries(event.target.value); setSoulTouched(true); }}
                  />
                </FormField>
              </div>
              <FormField label="表达语气" requirement="optional" htmlFor="editSoulTone" hint="稳定语气，不替代本轮用户要求。">
                <textarea
                  id="editSoulTone"
                  rows={3}
                  maxLength={1024}
                  value={soulTone}
                  onChange={event => { setSoulTone(event.target.value); setSoulTouched(true); }}
                />
              </FormField>
            </div>
          ) : null}
          <div className="source-provenance" role="status" aria-label="Soul 编译来源">
            <span><strong>结构化来源</strong><code>{detail.soulProjection?.source || "AgentSpec.soul"}</code></span>
            <span><strong>生效摘要</strong><code>{soulTouched ? "保存后重新计算" : detail.soulProjection?.digest || "未生成"}</code></span>
            <span><strong>编译位置</strong><code>{soulCompileTarget}</code></span>
          </div>
          <p className="agent-policy-note">
            {runtime === "codex"
              ? "ManagedRuntime 启动时会把 Soul 确定性编译到 base_instructions，并保留结构化源与 digest。"
              : runtime === "plugin"
                ? "Bundle 保留 instructions/soul.md；外部 Provider 按固定插件合同消费该审核快照。"
                : "构建时按 canonical JSON 计算 SHA-256，并编译到 resolved-agent-spec.instructions.system；Bundle 同时保留 instructions/soul.md。"}
          </p>
        </fieldset>
        <FormField
          label="系统提示词"
          requirement="required"
          htmlFor="editAgentPrompt"
          hint="首个 Agent 写入根目录 agentengine.yaml；后续 Agent 写入 agents/<id>/agentengine.yaml。"
          error={agentForm.formState.errors.prompt?.message}
        >
          <textarea id="editAgentPrompt" maxLength={32768} rows={10} {...agentForm.register("prompt")} />
        </FormField>
        </section>

        <section className="agent-edit-section" hidden={visibleSection !== 2} aria-label="能力绑定">
        <div className="agent-edit-section-heading">
          <span className="eyebrow">02</span>
          <div><h3>能力绑定</h3><p>配置模型、Skill、MCP 与 Runtime 支持的 Tool；切换分区不会丢失未保存修改。</p></div>
        </div>
        <div className="form-grid two-columns">
          <FormField
            label="默认模型"
            requirement="required"
            htmlFor="editDefaultModel"
            hint="每轮未指定模型时使用"
            footer={!selectedModelItems.length ? <span className="studio-field-hint">{preservesManifestModel ? `历史声明模型 ${fallbackModel} 将原样保留；从下方目录选择后可切换。` : "请先从模型 allowlist 中至少选择一个模型。"}</span> : null}
          >
            <StudioSelect
              id="editDefaultModel"
              ariaLabel="默认模型"
              value={defaultModel}
              placeholder={fallbackModel || "请先绑定模型"}
              disabled={!selectedModelItems.length}
              options={selectedModelItems.map(item => ({
                value: item.resourceId,
                label: item.displayName,
                description: modelName(item),
              }))}
              onValueChange={setDefaultModel}
            />
          </FormField>
        </div>
        <div className="field quick-model-binding-field">
          <div className="field-heading"><label>绑定模型</label><span className="helper">会话中只能动态切换到这里选中的模型</span></div>
          <StudioMultiSelect
            ariaLabel="选择绑定模型"
            items={visibleModels}
            selectedIds={selectedModels}
            getId={item => item.resourceId}
            getLabel={item => item.displayName}
            getDescription={item => modelName(item) !== item.displayName ? modelName(item) : ""}
            onChange={changeModels}
            searchPlaceholder="搜索绑定模型"
            emptyMessage="当前模型服务没有返回可绑定模型"
          />
        </div>
        <div className="field quick-model-binding-field">
          <div className="field-heading"><label>绑定 Skill / MCP</label><span className="helper">{runtime === "harness" ? "Skill 与 MCP 由 KsADK Harness 按需加载，并执行权限与审批策略。" : runtime === "codex" ? "Skill 与 MCP 由 Codex Runtime 按能力投影。" : runtime === "plugin" ? "Skill 与 MCP 会通过 PluginHost 投影给外部 Provider。" : "Skill 可编辑；当前 Runtime 尚未实现 MCP 源码注入，历史 MCP 仅保留。"}</span></div>
          <div className="quick-capability-bindings">
            <StudioMultiSelect
              ariaLabel="选择绑定 Skill"
              items={visibleSkills}
              selectedIds={selectedSkills}
              getId={item => item.resourceId}
              getLabel={item => item.displayName}
              getDescription={item => item.version}
              onChange={setSelectedSkills}
              searchPlaceholder="搜索 Skill"
              emptyMessage="没有已安装的 Skill"
            />
            <StudioMultiSelect
              ariaLabel="选择绑定 MCP"
              items={supportsMcpEditing ? visibleMcps : visibleMcps.filter(item => selectedMcp.includes(item.resourceId))}
              selectedIds={selectedMcp}
              getId={item => item.resourceId}
              getLabel={item => item.displayName}
              getDescription={item => mcpUnavailableReason(item, runtime) || `${item.version} · ${item.health?.toolCount || 0} Tool`}
              onChange={supportsMcpEditing ? setSelectedMcp : () => undefined}
              disabledIds={supportsMcpEditing ? visibleMcps.filter(item => !selectedMcp.includes(item.resourceId) && mcpUnavailableReason(item, runtime)).map(item => item.resourceId) : selectedMcp}
              searchPlaceholder="搜索 MCP"
              emptyMessage={supportsMcpEditing ? "没有已连接的 MCP" : "当前 Runtime 不支持新增 MCP"}
            />
          </div>
        </div>
        {runtime === "harness" && <SubAgentBindingsEditor value={subAgents} tools={visibleTools.filter(tool => selectedTools.includes(tool.resourceId) && tool.contract?.name).map(tool => ({ name: tool.contract!.name!, label: tool.displayName }))} onChange={value => { setSubAgents(value); setSubAgentsTouched(true); }} />}
        {runtime === "codex" && visibleSection === 2 && <NativePluginBindings
          key={`${agentId}-codex-plugins`}
          value={selectedPlugins.filter(binding => binding.ecosystem === "codex")}
          onChange={bindings => setSelectedPlugins([
            ...selectedPlugins.filter(binding => binding.ecosystem !== "codex"),
            ...bindings,
          ])}
          onPendingChange={setPluginsPending}
        />}
        {visibleSection === 2 && <PlatformResourceBindings
          key={`${agentId}-platform-resources`}
          value={selectedPlugins}
          onChange={bindings => {
            setSelectedPlugins(bindings);
            const platformMemory = bindings.find(binding =>
              (binding.config as any)?.binding?.resource?.kind === "memory-instance"
            );
            const bindingId = String((platformMemory?.config as any)?.binding?.id || "");
            if (bindingId) {
              setMemoryEnabled(true);
              setMemoryProviderRef(`binding://${bindingId}`);
              setMemoryRecallEnabled(true);
              setMemoryRecallMinScore(0);
              setMemoryWriteMode("off");
              setMemoryWriteRollout("off");
            } else if (memoryProviderRef.startsWith("binding://platform-memory-instance")) {
              setMemoryEnabled(false);
              setMemoryProviderRef("local-default");
            }
            setMemoryTouched(true);
          }}
          onPendingChange={setPluginsPending}
        />}
        {detail.bindingProjection?.unresolvedMcpServers?.length ? (
          <div className="inline-alert warning" role="status">
            <CircleAlert size={16} />
            <div>
              <strong>部分 YAML MCP 尚未进入资源目录</strong>
              <p>{detail.bindingProjection.unresolvedMcpServers.map(item => item.name).join("、")} 未映射到资源目录；保存时会原样保留，请在资源页接入后再可视化编辑。</p>
            </div>
          </div>
        ) : null}
        {!['codex', 'plugin'].includes(runtime) && <div className="field quick-model-binding-field">
          <div className="field-heading"><label>绑定 Tool</label><span className="helper">{["codex", "plugin"].includes(runtime) ? "当前 Runtime 不支持新增 ksadk Tool；历史绑定仅保留，不能修改。" : "仅展示当前 Runtime 合同允许的 ksadk Tool。"}</span></div>
          <StudioMultiSelect
            ariaLabel="选择绑定 Tool"
            items={visibleTools}
            selectedIds={selectedTools}
            getId={item => item.resourceId}
            getLabel={item => item.displayName}
            getDescription={item => item.version}
            onChange={setSelectedTools}
            disabledIds={[]}
            searchPlaceholder="搜索 Tool"
            emptyMessage="没有可绑定的 Tool"
          />
        </div>}
        </section>

        <section className="agent-edit-section" hidden={visibleSection !== 3} aria-label="运行策略">
        <div className="agent-edit-section-heading">
          <span className="eyebrow">03</span>
          <div><h3>运行策略</h3><p>配置跨会话记忆；Context 高级选项通常保持默认即可。</p></div>
        </div>
        {["adk", "langgraph"].includes(runtime) ? (
          <div className="form-grid two-columns agent-runtime-config-grid">
            <FormField label="项目相对路径" requirement="required" htmlFor="editRuntimeProjectPath" hint="相对于当前 Studio 工作区；保存后由新 Revision 构建。">
              <input id="editRuntimeProjectPath" value={runtimeProjectPath} onChange={event => setRuntimeProjectPath(event.target.value)} />
            </FormField>
            <FormField label="入口文件" requirement="required" htmlFor="editRuntimeEntryPoint" hint="ADK 或 LangGraph Agent 的 Python 入口文件。">
              <input id="editRuntimeEntryPoint" value={runtimeEntryPoint} onChange={event => setRuntimeEntryPoint(event.target.value)} />
            </FormField>
            <FormField label="Agent 变量" requirement="required" htmlFor="editRuntimeAgentVariable" hint="入口模块导出的 Agent 或 Graph 变量名。">
              <input id="editRuntimeAgentVariable" value={runtimeAgentVariable} onChange={event => setRuntimeAgentVariable(event.target.value)} />
            </FormField>
          </div>
        ) : null}
        <div className="form-grid two-columns agent-execution-config-grid">
          <FormField label="执行策略" requirement="required" htmlFor="editExecutionStrategy" hint="直接执行适合普通对话；计划执行适合多步骤任务。">
            <StudioSelect
              id="editExecutionStrategy"
              ariaLabel="执行策略"
              value={executionStrategy}
              options={[
                { value: "direct", label: "直接执行" },
                { value: "plan-act-observe", label: "计划 · 执行 · 观察" },
              ]}
              onValueChange={setExecutionStrategy}
            />
          </FormField>
          <FormField label="最大步骤数" requirement="required" htmlFor="editExecutionMaxSteps" hint="单次运行允许的最大 Agent 步骤，范围 1–100。">
            <input id="editExecutionMaxSteps" type="number" min={1} max={100} value={executionMaxSteps} onChange={event => setExecutionMaxSteps(Number(event.target.value))} />
          </FormField>
          <FormField label="超时秒数" requirement="required" htmlFor="editExecutionTimeout" hint="单次运行的整体超时，范围 1–3600 秒。">
            <input id="editExecutionTimeout" type="number" min={1} max={3600} value={executionTimeoutSeconds} onChange={event => setExecutionTimeoutSeconds(Number(event.target.value))} />
          </FormField>
        </div>
        <fieldset className="agent-policy-editor memory-policy-editor">
          <legend>Memory · 跨会话策略</legend>
          <label className="pcm-memory-toggle">
            <input
              type="checkbox"
              checked={memoryEnabled}
              onChange={event => {
                setMemoryEnabled(event.target.checked);
                setMemoryTouched(true);
              }}
            />
            <span>
              <strong>{memoryEnabled ? "已启用跨会话记忆" : "未启用跨会话记忆"}</strong>
              <small>启用状态、召回与写入分别受下列 AgentVersion 策略控制；不会因打开编辑页而改写旧配置。</small>
            </span>
          </label>
          <div className="form-grid two-columns memory-policy-grid">
            <FormField label="Memory Provider" requirement="required" htmlFor="editMemoryProvider" hint="只保存 providerRef，不保存凭证。">
              <input
                id="editMemoryProvider"
                maxLength={128}
                value={memoryProviderRef}
                onChange={event => { setMemoryProviderRef(event.target.value); setMemoryTouched(true); }}
              />
            </FormField>
            <FormField label="写入模式" requirement="required" htmlFor="editMemoryWriteMode" hint="候选模式仍需策略审核；显式模式只接受用户确认的写入。">
              <StudioSelect
                id="editMemoryWriteMode"
                ariaLabel="Memory 写入模式"
                value={memoryWriteMode}
                options={[
                  { value: "off", label: "不产生写入候选" },
                  { value: "explicit_only", label: "仅显式写入" },
                  { value: "candidate", label: "候选写入" },
                ]}
                onValueChange={value => { setMemoryWriteMode(value); setMemoryTouched(true); }}
              />
            </FormField>
            <FormField label="写入 Rollout" requirement="required" htmlFor="editMemoryWriteRollout" hint="关闭、仅观察和正式启用由版本策略明确区分。">
              <StudioSelect
                id="editMemoryWriteRollout"
                ariaLabel="Memory 写入 Rollout"
                value={memoryWriteRollout}
                options={[
                  { value: "off", label: "关闭" },
                  { value: "shadow", label: "仅观察" },
                  { value: "enabled", label: "正式启用" },
                ]}
                onValueChange={value => { setMemoryWriteRollout(value); setMemoryTouched(true); }}
              />
            </FormField>
            <FormField label="召回开关" requirement="required" htmlFor="editMemoryRecallEnabled" hint="可启用 Memory 但关闭自动召回。">
              <div className="inline-checkbox-control">
                <input
                  id="editMemoryRecallEnabled"
                  type="checkbox"
                  checked={memoryRecallEnabled}
                  onChange={event => { setMemoryRecallEnabled(event.target.checked); setMemoryTouched(true); }}
                />
                <span>{memoryRecallEnabled ? "允许按策略召回" : "不自动召回"}</span>
              </div>
            </FormField>
            <FormField label="召回 Token 上限" requirement="required" htmlFor="editMemoryRecallMaxTokens" hint="允许为 0，表示不投影召回正文。">
              <input id="editMemoryRecallMaxTokens" type="number" min={0} value={memoryRecallMaxTokens} onChange={event => { setMemoryRecallMaxTokens(Number(event.target.value)); setMemoryTouched(true); }} />
            </FormField>
            <FormField label="召回条数" requirement="required" htmlFor="editMemoryRecallTopK" hint="范围 1–64。">
              <input id="editMemoryRecallTopK" type="number" min={1} max={64} value={memoryRecallTopK} onChange={event => { setMemoryRecallTopK(Number(event.target.value)); setMemoryTouched(true); }} />
            </FormField>
            <FormField label="最小相关度" requirement="required" htmlFor="editMemoryRecallMinScore" hint="范围 0–1。">
              <input id="editMemoryRecallMinScore" type="number" min={0} max={1} step={0.05} value={memoryRecallMinScore} onChange={event => { setMemoryRecallMinScore(Number(event.target.value)); setMemoryTouched(true); }} />
            </FormField>
            <FormField label="压缩前刷新" requirement="optional" htmlFor="editMemoryFlush" hint="在上下文压缩前提交已审核的 Memory 候选。">
              <div className="inline-checkbox-control">
                <input id="editMemoryFlush" type="checkbox" checked={memoryFlushBeforeCompaction} onChange={event => { setMemoryFlushBeforeCompaction(event.target.checked); setMemoryTouched(true); }} />
                <span>{memoryFlushBeforeCompaction ? "压缩前刷新" : "不在压缩前刷新"}</span>
              </div>
            </FormField>
          </div>
          <div className="source-provenance memory-source-provenance" role="status" aria-label="Memory 策略来源">
            <span><strong>策略来源</strong><code>AgentSpec.memory</code></span>
            <span><strong>Provider</strong><code>{memoryProviderRef || "未配置"}</code></span>
            <span><strong>写入生效</strong><code>Context.rollout.memoryWrite={memoryWriteRollout}</code></span>
          </div>
        </fieldset>
        <details className="pcm-policy-card">
          <summary>
            <span><strong>运行上下文（高级）</strong><small>调整 Context 责任边界和优化策略；不确定时保持自动与仅观察</small></span>
          </summary>
          <div className="pcm-policy-body">
            <div className="form-grid two-columns">
              <FormField label="上下文管理方式" requirement="optional" htmlFor="editContextOwnership" hint="决定由平台、框架或原生 Runtime 负责最终模型输入。">
                <StudioSelect
                  id="editContextOwnership"
                  ariaLabel="上下文管理方式"
                  value={contextOwnership}
                  options={contextOwnershipOptions}
                  onValueChange={setContextOwnership}
                />
              </FormField>
              <FormField label="上下文优化" requirement="optional" htmlFor="editContextEngineRollout" hint="仅观察只生成诊断证据；正式启用会执行预算、压缩和降载。">
                <StudioSelect
                  id="editContextEngineRollout"
                  ariaLabel="Context Engine"
                  value={contextEngineRollout}
                  options={[
                    { value: "off", label: "使用 Runtime 默认行为" },
                    { value: "shadow", label: "仅观察（推荐）" },
                    { value: "enabled", label: "正式启用" },
                  ]}
                  onValueChange={setContextEngineRollout}
                />
              </FormField>
            </div>
          </div>
        </details>
        </section>
        <div className="quick-create-actions">
          <label className="checkbox-row">
            <input type="checkbox" checked={buildAfterSave} onChange={event => setBuildAfterSave(event.target.checked)} />
            <span><strong>{isManagedDeclaration ? "保存后生成配置快照" : "保存后构建新 Bundle"}</strong><small>{isManagedDeclaration ? "校验 YAML 并生成可追溯的部署输入" : "新 Bundle 完成后进入会话工作台"}</small></span>
          </label>
          {onCancel && <button className="button secondary" type="button" disabled={saving} onClick={onCancel}>取消</button>}
          <button className="button accent" type="submit" disabled={saving || pluginsPending}><Package size={15} /><span>{saving ? "正在保存" : "保存修改"}</span></button>
        </div>
        {saveError && <div className="inline-alert error"><CircleAlert size={16} /><div><strong>操作未完成</strong><p>{saveError}</p></div></div>}
      </form>
      </FormProvider>
      <details className="manifest-preview">
        <summary>查看配置源码</summary>
        <CodeViewer code={manifest} language="yaml" filename="agentkit.yaml" wrap />
        <div className="manifest-contract">
          <span><Check size={13} />唯一配置源</span>
          <span><Check size={13} />SHA-256 可追溯</span>
          <span><Check size={13} />RuntimeAdapter 执行</span>
        </div>
      </details>
    </div>
  );
}

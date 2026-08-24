import { useEffect, useMemo, useState } from "react";
import { Check, CircleAlert, Code, Package } from "lucide-react";
import { zodResolver } from "@hookform/resolvers/zod";
import { FormProvider, useForm, type Resolver } from "react-hook-form";
import { apiFetch } from "../api";
import { showToast } from "../components/Toast";
import { AgentAppearanceEditor } from "../components/AgentAppearanceEditor";
import type { AgentAppearance } from "../components/AgentAvatar";
import { FormField } from "../components/ui/FormField";
import { StudioMultiSelect } from "../components/ui/StudioMultiSelect";
import { StudioSelect } from "../components/ui/StudioSelect";
import { CodeViewer } from "../components/ui/CodeViewer";
import { applyApiFieldErrors } from "../lib/formErrors";
import { agentEditSchema, type AgentEditFormValues } from "../schemas/agentForms";

export interface EditorCatalogItem {
  resourceId: string;
  kind: string;
  name: string;
  displayName: string;
  version: string;
  status: string;
  contract?: { model?: string; executor?: string };
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
      };
      instructions?: { system?: string; task?: string };
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
        recall?: { enabled?: boolean; [key: string]: unknown };
        write?: { mode?: string; [key: string]: unknown };
        [key: string]: unknown;
      };
      bindings?: {
        modelProfileId?: string | null;
        modelProfileIds?: string[];
        skills?: CapabilityBindingValue[];
        mcpServers?: CapabilityBindingValue[];
        tools?: CapabilityBindingValue[];
        [key: string]: unknown;
      };
      [key: string]: unknown;
    };
  };
  bindingProjection?: {
    unresolvedMcpServers?: Array<{ name: string; reason: string }>;
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
  if (runtime === "adk") return "ADKRuntimeAdapter";
  if (runtime === "langgraph") return "LangGraphRuntimeAdapter";
  return "CodexRuntimeAdapter";
}

function runtimeManifest(runtime: string) {
  if (runtime === "codex") return { type: "codex" };
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
  activeSection = 1,
  onSaved,
  onAppearanceSaved,
}: {
  agentId: string;
  catalog: EditorCatalogItem[];
  activeSection?: number;
  onSaved: (agentId: string, openChat: boolean) => void;
  onAppearanceSaved?: () => void;
}) {
  const [detail, setDetail] = useState<AgentDetail | null>(null);
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
  const [visibleSection, setVisibleSection] = useState(activeSection);
  const [runtimeProjectPath, setRuntimeProjectPath] = useState(".");
  const [runtimeEntryPoint, setRuntimeEntryPoint] = useState("");
  const [runtimeAgentVariable, setRuntimeAgentVariable] = useState("root_agent");
  const [executionStrategy, setExecutionStrategy] = useState("direct");
  const [executionMaxSteps, setExecutionMaxSteps] = useState(12);
  const [executionTimeoutSeconds, setExecutionTimeoutSeconds] = useState(120);
  const [contextOwnership, setContextOwnership] = useState("auto");
  const [contextEngineRollout, setContextEngineRollout] = useState("shadow");
  const [memoryEnabled, setMemoryEnabled] = useState(false);
  const [memoryWriteRollout, setMemoryWriteRollout] = useState("off");
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
        setRuntimeProjectPath(String(draft.spec?.runtime?.projectPath || "."));
        setRuntimeEntryPoint(String(draft.spec?.runtime?.entryPoint || (draft.spec?.runtime?.type === "langgraph" ? "graph.py" : "agent.py")));
        setRuntimeAgentVariable(String(draft.spec?.runtime?.agentVariable || (draft.spec?.runtime?.type === "langgraph" ? "app" : "root_agent")));
        setExecutionStrategy(String(draft.spec?.execution?.strategy || "direct"));
        setExecutionMaxSteps(Number(draft.spec?.execution?.maxSteps ?? 12));
        setExecutionTimeoutSeconds(Number(draft.spec?.execution?.timeoutSeconds ?? 120));
        setContextOwnership(String(draft.spec?.context?.ownership || "auto"));
        setContextEngineRollout(String(draft.spec?.context?.rollout?.contextEngine || "shadow"));
        setMemoryEnabled(Boolean(draft.spec?.memory?.enabled && draft.spec?.memory?.recall?.enabled));
        setMemoryWriteRollout(String(draft.spec?.context?.rollout?.memoryWrite || "off"));
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
  const contextOwnershipOptions = runtime === "codex"
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
  const manifest = runtime === "codex" ? [
    `name: ${slug}`,
    "version: 1.0.0",
    "framework: codex",
    "artifact_type: ManagedRuntime",
    "runtime:",
    "  name: codex",
    "  version: 0.144.4",
    `model: ${modelName(primaryModel) || fallbackModel}`,
    ...(manifestModels.length > 1 ? ["models:", ...manifestModels.map(item => `  - ${item}`)] : []),
    "prompt: |-",
    ...prompt.split("\n").map(line => `  ${line}`),
  ].join("\n") : [
    "apiVersion: agentkit.ksyun.com/v1alpha1",
    "kind: Agent",
    "metadata:",
    `  id: ${slug}`,
    "spec:",
    "  runtime:",
    `    type: ${runtime}`,
    `    projectPath: ${runtimeProjectPath || "."}`,
    `    entryPoint: ${runtimeEntryPoint || (runtime === "adk" ? "agent.py" : "graph.py")}`,
    `    agentVariable: ${runtimeAgentVariable || (runtime === "adk" ? "root_agent" : "app")}`,
    "  instructions:",
    "    system: |-",
    ...prompt.split("\n").map(line => `      ${line}`),
  ].join("\n");

  async function save(values: AgentEditFormValues) {
    if (!detail || saving) return;
    const resolvedDefaultModel = defaultModel || selectedModels[0] || "";
    if (!resolvedDefaultModel && !preservesManifestModel) {
      setSaveError("请至少绑定一个模型并设置为默认模型");
      return;
    }
    if (values.runtimeType !== "codex" && (!runtimeProjectPath.trim() || !runtimeEntryPoint.trim() || !runtimeAgentVariable.trim())) {
      setSaveError("请完整填写项目相对路径、入口文件和 Agent 变量");
      return;
    }
    if (!Number.isInteger(executionMaxSteps) || executionMaxSteps < 1 || executionMaxSteps > 100) {
      setSaveError("最大步骤数必须是 1 到 100 的整数");
      return;
    }
    if (!Number.isInteger(executionTimeoutSeconds) || executionTimeoutSeconds < 1 || executionTimeoutSeconds > 3600) {
      setSaveError("超时秒数必须是 1 到 3600 的整数");
      return;
    }
    setSaving(true);
    setSaveError("");
    try {
      const original = detail.draft.spec;
      const spec = JSON.parse(JSON.stringify(original));
      spec.runtime = {
        ...runtimeManifest(values.runtimeType),
        ...(original.runtime || {}),
        type: values.runtimeType,
        ...(values.runtimeType === "codex" ? {} : {
          projectPath: runtimeProjectPath.trim(),
          entryPoint: runtimeEntryPoint.trim(),
          agentVariable: runtimeAgentVariable.trim(),
        }),
      };
      spec.instructions = {
        ...(original.instructions || {}),
        system: values.prompt.trim(),
        task: original.instructions?.task || "",
      };
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
          memoryWrite: memoryEnabled ? "enabled" : "off",
        },
      };
      spec.memory = {
        ...(original.memory || {}),
        enabled: memoryEnabled,
        recall: { ...(original.memory?.recall || {}), enabled: memoryEnabled },
        write: { ...(original.memory?.write || {}), mode: "candidate" },
      };
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
        if (applyApiFieldErrors(saved, agentForm.setError)) return;
        throw new Error(saved?.error?.message || `保存失败（${response.status}）`);
      }
      const savedId = saved?.metadata?.id || agentId;
      showToast(
        "Agent 已更新",
        isManagedDeclaration
          ? "本地配置已保存；更新云端后生效。"
          : "本地声明已保存；已部署版本不会静默改变。",
      );

      if (buildAfterSave) {
        const revision = saved?.metadata?.revision || detail.draft.metadata.revision + 1;
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
      setSaveError(error.message || "保存失败");
      showToast("保存失败", error.message || "保存失败", "error");
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
        <div className="quick-runtime-strip">
          <span className="runtime-logo"><Code size={17} /></span>
          <div><strong>{runtimeTitle(runtime)}</strong><span>一 Agent 一 YAML · 不可变 Bundle</span></div>
          <span className="badge" data-state="ready">本地可运行</span>
        </div>
        <div className="quick-create-heading">
          <span className="eyebrow">YAML-first</span>
          <h2 title={slug}>编辑 {name || detail.draft.metadata.name}</h2>
          <p>保存会直接回写该 Agent 的 agentengine.yaml；旧构建会标记为过期。</p>
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
        <div className="callout compact agent-version-boundary">
          <div>
            <strong>{isManagedDeclaration ? "配置修订边界" : "部署版本边界"}</strong>
            <p>{isManagedDeclaration
              ? "本页保存本地 YAML 配置；已部署版本不会自动改变，执行云端更新后才会生效。"
              : "本页保存 Prompt、模型与能力绑定。Runtime 类型不可直接切换；代码入口等修改会进入新 Revision，并按运行时能力生成新 Bundle。"}</p>
          </div>
        </div>
        <section className="agent-edit-section" hidden={visibleSection !== 1} aria-label="基础与 Prompt">
        <div className="agent-edit-section-heading">
          <span className="eyebrow">01</span>
          <div><h3>基础与 Prompt</h3><p>维护 Agent 身份、Runtime 与系统提示词。</p></div>
        </div>
        <AgentAppearanceEditor
          name={name || detail.draft.metadata.name}
          appearance={detail.draft.metadata.appearance}
          disabled={saving}
          onSave={saveAppearance}
        />
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
              { value: "codex", label: "CodexRuntimeAdapter" },
              { value: "adk", label: "ADKRuntimeAdapter" },
              { value: "langgraph", label: "LangGraphRuntimeAdapter" },
            ]}
            onValueChange={() => undefined}
          />
        </FormField>
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
            getDescription={item => `${modelName(item)} · ${item.status}`}
            onChange={changeModels}
            searchPlaceholder="搜索绑定模型"
            emptyMessage="当前模型服务没有返回可绑定模型"
          />
        </div>
        <div className="field quick-model-binding-field">
          <div className="field-heading"><label>绑定 Skill / MCP</label><span className="helper">{runtime === "codex" ? "Skill 与 MCP 由 Codex Runtime 按能力投影。" : "Skill 可编辑；当前 Runtime 尚未实现 MCP 源码注入，历史 MCP 仅保留。"}</span></div>
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
              items={runtime === "codex" ? visibleMcps : visibleMcps.filter(item => selectedMcp.includes(item.resourceId))}
              selectedIds={selectedMcp}
              getId={item => item.resourceId}
              getLabel={item => item.displayName}
              getDescription={item => `${item.version} · ${item.health?.toolCount || 0} Tool`}
              onChange={runtime === "codex" ? setSelectedMcp : () => undefined}
              disabledIds={runtime === "codex" ? [] : selectedMcp}
              searchPlaceholder="搜索 MCP"
              emptyMessage={runtime === "codex" ? "没有已连接的 MCP" : "当前 Runtime 不支持新增 MCP"}
            />
          </div>
        </div>
        {detail.bindingProjection?.unresolvedMcpServers?.length ? (
          <div className="inline-alert warning" role="status">
            <CircleAlert size={16} />
            <div>
              <strong>部分 YAML MCP 尚未进入资源目录</strong>
              <p>{detail.bindingProjection.unresolvedMcpServers.map(item => item.name).join("、")} 未映射到资源目录；保存时会原样保留，请在资源页接入后再可视化编辑。</p>
            </div>
          </div>
        ) : null}
        <div className="field quick-model-binding-field">
          <div className="field-heading"><label>绑定 Tool</label><span className="helper">{runtime === "codex" ? "当前 Runtime 不支持新增 ksadk Tool；历史绑定仅保留，不能修改。" : "仅展示当前 Runtime 合同允许的 ksadk Tool。"}</span></div>
          <StudioMultiSelect
            ariaLabel="选择绑定 Tool"
            items={runtime === "codex" ? visibleTools.filter(item => selectedTools.includes(item.resourceId)) : visibleTools}
            selectedIds={selectedTools}
            getId={item => item.resourceId}
            getLabel={item => item.displayName}
            getDescription={item => item.version}
            onChange={runtime === "codex" ? () => undefined : setSelectedTools}
            disabledIds={runtime === "codex" ? selectedTools : []}
            searchPlaceholder="搜索 Tool"
            emptyMessage={runtime === "codex" ? "Codex 使用原生工具" : "没有可绑定的 Tool"}
          />
        </div>
        </section>

        <section className="agent-edit-section" hidden={visibleSection !== 3} aria-label="运行策略">
        <div className="agent-edit-section-heading">
          <span className="eyebrow">03</span>
          <div><h3>运行策略</h3><p>配置跨会话记忆；Context 高级选项通常保持默认即可。</p></div>
        </div>
        {runtime !== "codex" ? (
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
        <div className="field quick-model-binding-field">
          <div className="field-heading"><label>跨会话记忆</label><span className="helper">保存稳定事实，并在后续会话按需召回</span></div>
          <label className="pcm-memory-toggle">
            <input
              type="checkbox"
              checked={memoryEnabled}
              onChange={event => {
                setMemoryEnabled(event.target.checked);
                setMemoryWriteRollout(event.target.checked ? "enabled" : "off");
              }}
            />
            <span>
              <strong>{memoryEnabled ? "已启用跨会话记忆" : "未启用跨会话记忆"}</strong>
              <small>{memoryEnabled
                ? memoryWriteRollout === "enabled"
                  ? "新记忆会通过策略检查后保存。"
                  : "当前旧配置仅召回或观察；保存修改后将正式启用记忆写入。"
                : "当前会话内容不会写入长期记忆。"}</small>
            </span>
          </label>
        </div>
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
          <button className="button accent" type="submit" disabled={saving}><Package size={15} /><span>{saving ? "正在保存" : "保存修改"}</span></button>
        </div>
        {saveError && <div className="inline-alert error"><CircleAlert size={16} /><div><strong>保存失败</strong><p>{saveError}</p></div></div>}
      </form>
      </FormProvider>
      <aside className="manifest-preview">
        <CodeViewer code={manifest} language="yaml" filename="agentkit.yaml" wrap />
        <div className="manifest-contract">
          <span><Check size={13} />唯一配置源</span>
          <span><Check size={13} />SHA-256 可追溯</span>
          <span><Check size={13} />RuntimeAdapter 执行</span>
        </div>
      </aside>
    </div>
  );
}

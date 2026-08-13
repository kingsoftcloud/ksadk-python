import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  ArrowLeft, ArrowRight, Zap, MessagesSquare, Upload, Folder, Check, Copy,
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
import { FormField } from "../components/ui/FormField";
import { StudioDrawer } from "../components/ui/StudioDialog";
import { CodeViewer } from "../components/ui/CodeViewer";
import { applyApiFieldErrors } from "../lib/formErrors";
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

export function CreatePage({ editingAgentId, viewportMode, onBack, onCreated, onAgentsChanged }: {
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
  const [promptStatus, setPromptStatus] = useState<"idle" | "composing" | "done">("idle");
  const [createError, setCreateError] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [summaryOpen, setSummaryOpen] = useState(false);
  const [configModel, setConfigModel] = useState<ResItem | null>(null);
  const [showMcpConnect, setShowMcpConnect] = useState(false);
  const compositionRef = useRef<any>(null);
  const createRailTriggerRef = useRef<HTMLButtonElement>(null);
  const composeSeq = useRef(0);
  const conversationEntryInitialized = useRef(false);

  /* conversation 模式 */
  const [convMessages, setConvMessages] = useState<Array<{ role: string; content: string }>>([]);
  const [convInput, setConvInput] = useState("");
  const [convModels, setConvModels] = useState<string[]>([]);
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
      const [d, discovered] = await Promise.all([
      apiFetch("/api/v1/catalog/resources?limit=200").then(r => r.json()),
      apiFetch("/api/v1/catalog/models").then(r => r.json()).catch(() => null),
      ]);
      const items: ResItem[] = d.items || [];
      const localModels = items.filter(i => i.kind === "model" && (i.source === "local" || i.source === "market"));
      const modelItems = discovered?.items?.length ? [...localModels, ...discovered.items] : items.filter(i => i.kind === "model");
      setCatalog([...modelItems, ...items.filter((i: ResItem) => i.kind !== "model")]);

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

  const models = useMemo(() => catalog.filter(i => i.kind === "model" && ["ready", "missing-secret"].includes(i.status)), [catalog]);
  const tools = useMemo(() => catalog.filter(i => i.kind === "tool" && i.status === "ready"), [catalog]);
  const mcps = useMemo(() => catalog.filter(i => i.kind === "mcp"), [catalog]);
  const skills = useMemo(() => catalog.filter(i => i.kind === "skill" && i.status === "ready"), [catalog]);
  const resourceById = useCallback((id: string) => catalog.find(i => i.resourceId === id), [catalog]);
  const credentialOf = useCallback((item?: ResItem) => {
    const ref = credentialReference(item);
    return ref ? credentialStatuses[ref] : undefined;
  }, [credentialStatuses]);

  useEffect(() => {
    if (mode !== "conversation") {
      conversationEntryInitialized.current = false;
      return;
    }
    if (conversationEntryInitialized.current || !models.length) return;
    conversationEntryInitialized.current = true;
    if (!convModels.length) setConvModels([models[0].resourceId]);
  }, [mode, models, convModels.length]);

  /* 草稿（localStorage） */
  function draftKey() { return `${DRAFT_PREFIX}:local-workspace`; }
  function saveDraft() {
    try {
      window.localStorage.setItem(draftKey(), JSON.stringify({
        version: 1, savedAt: new Date().toISOString(), mode,
        wizard: { step, maxStep, template, runtime, depth, selectedTools, selectedSkills, selectedMcp, selectedModels, policy },
        fields: { name, slug, description, prompt, audience, language, format, systemPrompt, taskPrompt, buildAfterCreate },
      }));
      setDraftState(`已保存 ${new Intl.DateTimeFormat("zh-CN", { hour: "2-digit", minute: "2-digit" }).format(new Date())}`);
    } catch { /* 存储不可用时静默 */ }
  }
  function markDirty() { setDraftState("有未保存更改"); }

  /* 向导 compose */
  const wizardPayload = useCallback(() => ({
    prompt,
    goal: prompt,
    description,
    taskPrompt,
    audience,
    language,
    depth,
    outputFormat: format,
    modelProfileId: selectedModels[0] || null,
    modelProfileIds: selectedModels,
    toolResourceIds: selectedTools,
    skillResourceIds: selectedSkills,
    mcpResourceIds: selectedMcp,
    policyTemplate: policy,
    executionStrategy: template === "research" ? "plan-act-observe" : "direct",
    maxSteps: template === "research" ? 28 : 12,
    timeoutSeconds: template === "research" ? 900 : 120,
  }), [prompt, description, taskPrompt, template, audience, language, depth, format, selectedModels, selectedTools, selectedSkills, selectedMcp, policy]);

  const composeAgent = useCallback(async ({ preservePrompt = true } = {}) => {
    const seq = ++composeSeq.current;
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
      setSelectedTools(runtime === "codex" ? [] : (b.tools || []).map((i: any) => i.resourceId));
      setSelectedSkills((b.skills || []).map((i: any) => i.resourceId));
      setSelectedMcp((b.mcpServers || []).map((i: any) => i.resourceId));
      const ids = b.modelProfileIds?.length ? b.modelProfileIds : b.modelProfileId ? [b.modelProfileId] : [];
      if (ids.length) setSelectedModels(ids);
      if (!preservePrompt || !systemPrompt.trim()) {
        quickForm.setValue("systemPrompt", composition.spec?.instructions?.system || "", { shouldDirty: true });
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
  }, [template, wizardPayload, runtime, systemPrompt, taskPrompt, quickForm]);

  async function gotoStep(next: number) {
    if (next < 1 || next > 4) return;
    if (next > step) {
      if (step === 1) {
        const valid = await quickForm.trigger(["name", "slug", "runtimeType", "prompt", "audience"]);
        if (!valid) { setCreateError("请修正标记字段后继续。"); return; }
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
        if (applyApiFieldErrors(d, quickForm.setError)) {
          setStep(1);
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
    if (!input || !convModels.length) {
      setConvError("请输入需求并选择用于构建的模型。");
      return;
    }
    setConvError("");
    const next = [...convMessages, { role: "user", content: input }];
    setConvMessages(next);
    setConvInput("");
    setConvBusy(true);
    try {
      const res = await apiFetch("/api/v1/authoring/conversations:compose", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ messages: next, modelProfileId: convModels[0] }),
      });
      const d = await res.json();
      if (!res.ok) throw new Error(d?.error?.message || `生成失败（${res.status}）`);
      setProposal(d.proposal);
      conversationForm.reset({
        name: d.proposal.name || "",
        slug: generateAgentSlug(),
        runtimeType: d.proposal.runtimeType || "codex",
        prompt: d.proposal.instructions?.system || "",
        description: d.proposal.description || "",
        modelProfileId: convModels[0],
      });
      setConvMessages([...next, { role: "assistant", content: JSON.stringify(d.proposal) }]);
    } catch (e: any) {
      setConvError(e.message || "对话构建失败");
    } finally {
      setConvBusy(false);
    }
  }

  async function confirmConversation(values: ConversationCommitFormValues) {
    if (!proposal) return;
    setConvBusy(true);
    setConvError("");
    try {
      const res = await apiFetch("/api/v1/authoring/quick", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          name: values.name.trim(), slug: values.slug.trim(), runtimeType: values.runtimeType,
          description: values.description?.trim() || proposal.description || "",
          spec: {
            instructions: { system: values.prompt.trim(), task: proposal.instructions?.task || "" },
            bindings: { modelProfileId: convModels[0] || null, modelProfileIds: convModels.slice() },
          },
        }),
      });
      const d = await res.json().catch(() => null);
      if (!res.ok) {
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
  const runtimeLabel = ({ codex: "Codex", adk: "ADK", langgraph: "LangGraph" } as Record<string, string>)[runtime] || runtime;
  const policyMeta = POLICY_META[policy];
  const reviewModel = selectedModels.map(id => resourceById(id)?.displayName || id).join("、") || "待选择";
  const capCount = selectedTools.length + selectedMcp.length + selectedSkills.length;
  const selectedModelItems = selectedModels.map(resourceById).filter((item): item is ResItem => Boolean(item));
  const selectedModelStatus = selectedModelItems.length === 0
    ? "未选择模型；Agent 可以先构建，但运行前需要配置。"
    : selectedModelItems.every(item => credentialOf(item)?.configured)
      ? `已选 ${selectedModelItems.length} 个模型 · 凭证已配置`
      : "部分模型凭证未配置；Agent 可以先构建，但运行前需要配置 API Key。";

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
  const scrollMode = !editingAgentId && mode === "conversation" ? "workbench" : "document";

  const renderCreateRailContent = (showModeLabel: boolean) => (
    <div className="create-rail-panel">
      {showModeLabel && <div className="create-rail-label">创建方式</div>}
      {!editingAgentId && (
        <nav className="authoring-mode-tabs" aria-label="创建方式">
          {MODE_TABS.map(tab => {
            const Icon = tab.icon;
            return (
              <button key={tab.id} className={mode === tab.id ? "active" : ""} type="button" onClick={() => selectMode(tab.id)}>
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
            {WIZARD_STEP_META.map((meta, index) => {
              const number = index + 1;
              return (
                <button
                  key={number}
                  className={`wizard-step${(editingAgentId ? number === 1 : step === number) ? " active" : ""}${!editingAgentId && number < maxStep && number !== step ? " completed" : ""}`}
                  type="button"
                  disabled={Boolean(editingAgentId) || number > maxStep}
                  onClick={() => gotoStep(number)}
                >
                  <span className="step-number">{number}</span>
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
    <div className="create-shell" data-scroll-mode={scrollMode}>
      <header className="create-header">
        <button className="button tertiary" type="button" onClick={onBack}>
          <ArrowLeft size={16} /><span>返回 Agent</span>
        </button>
        <div className="create-heading">
          <span className="eyebrow">Agent Builder</span>
          <h1>{editingAgentId ? "编辑 Agent" : "创建 Agent"}</h1>
          <p>{editingAgentId
            ? "修改系统提示词与模型绑定；本地标识保持不变，避免破坏已有引用。"
            : "从系统提示词开始，按需组合模型、Tool、MCP 与 Skill。"}</p>
        </div>
        <button
          ref={createRailTriggerRef}
          className="button secondary compact-create-rail-trigger"
          type="button"
          aria-expanded={createRailOpen}
          aria-controls="createRail"
          onClick={() => setCreateRailOpen(open => !open)}
        >
          创建方式
        </button>
        <div className="draft-state"><span className="status-dot neutral" /><span>{draftState}</span></div>
      </header>

      <div className="create-workbench">
        {viewportMode !== "compact" && (
          <aside id="createRail" className="create-rail" aria-label="创建方式与步骤">
            {renderCreateRailContent(true)}
          </aside>
        )}

        {viewportMode === "compact" && createRailOpen && (
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
              onSaved={(id, openChat) => onCreated(id, openChat)}
              onAppearanceSaved={onAgentsChanged}
            />
          )}
          {!editingAgentId && mode === "conversation" && (
            <section className="authoring-mode-panel">
              <div className="authoring-panel-heading">
                <div><span className="eyebrow">Conversation authoring</span><h2>通过多轮对话设计 Agent</h2><p>模型只返回结构化 Draft Patch；确认前不会写入工作区。</p></div>
                <span className="status-badge neutral">Inspect → Confirm</span>
              </div>
              <div className="conversation-authoring-layout">
                <div className="authoring-chat-column">
                  <div className="authoring-transcript">
                    {convMessages.length === 0 && <div className="trace-stage-empty compact"><p>说明 Agent 的职责、边界、Runtime 和期望能力。</p></div>}
                    {convMessages.map((m, i) => (
                      <div key={i} className={`authoring-message ${m.role}`}>
                        <strong>{m.role === "user" ? "你" : "构建助手"}</strong>
                        <p>{m.role === "user" ? m.content : "已生成结构化方案，见右侧 Draft Patch。"}</p>
                      </div>
                    ))}
                  </div>
                  <div className="authoring-composer"><textarea rows={3} placeholder="例如：做一个 ADK 发布评审 Agent，只输出阻断项和证据" value={convInput} onChange={e => setConvInput(e.target.value)} /></div>
                  <div className="authoring-composer-actions">
                    <button className="button accent" type="button" disabled={convBusy} onClick={sendConversation}>
                      <Send size={16} /><span>{convBusy ? "正在生成" : "生成方案"}</span>
                    </button>
                  </div>
                  {convError && <div className="inline-alert error"><CircleAlert size={16} /><div><strong>对话构建失败</strong><p>{convError}</p></div></div>}
                  <div className="field authoring-model-field">
                    <label>用于构建的模型（可多选）</label>
                    <StudioMultiSelect
                      ariaLabel="选择用于构建的模型"
                      items={models}
                      selectedIds={convModels}
                      getId={item => item.resourceId}
                      getLabel={item => item.displayName}
                      getDescription={item => `${item.contract?.model || item.name} · ${credentialOf(item)?.configured ? "凭证已配置" : "需配置凭证"}`}
                      onChange={setConvModels}
                      searchPlaceholder="搜索构建模型"
                      emptyMessage="没有可用模型"
                    />
                  </div>
                </div>
                <FormProvider {...conversationForm}>
                <form className="authoring-inspection-card" onSubmit={conversationForm.handleSubmit(confirmConversation)} noValidate>
                  <div className="authoring-preview-heading">
                    <div><strong>Draft Patch</strong><span>必须确认后才创建</span></div>
                    <span className={`status-dot ${proposal ? "success" : "neutral"}`} />
                  </div>
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
                  <FormField label="Runtime" requirement="required" htmlFor="conversationRuntime" error={conversationForm.formState.errors.runtimeType?.message}>
                    <StudioSelect
                      id="conversationRuntime"
                      ariaLabel="Runtime"
                      value={conversationForm.watch("runtimeType")}
                      options={RUNTIME_OPTIONS}
                      onValueChange={value => conversationForm.setValue("runtimeType", value as ConversationCommitFormValues["runtimeType"], { shouldDirty: true, shouldValidate: true })}
                    />
                  </FormField>
                  <FormField label="系统提示词" requirement="required" htmlFor="conversationPrompt" error={conversationForm.formState.errors.prompt?.message}>
                    <textarea id="conversationPrompt" rows={8} {...conversationForm.register("prompt")} />
                  </FormField>
                  <CodeViewer
                    code={proposal ? JSON.stringify(proposal, null, 2) : "完成一轮或多轮对话后，这里会出现可编辑的 Draft Patch。"}
                    language={proposal ? "json" : "text"}
                    filename="draft-patch.json"
                    showLineNumbers={Boolean(proposal)}
                    wrap={!proposal}
                  />
                  <button className="button accent" type="submit" disabled={!proposal || convBusy}><Check size={16} /><span>确认并创建 Revision</span></button>
                </form>
                </FormProvider>
              </div>
            </section>
          )}

          {!editingAgentId && mode === "import" && (
            <section className="authoring-mode-panel">
              <div className="authoring-panel-heading">
                <div><span className="eyebrow">Agent import</span><h2>检查并导入 Agent</h2><p>先解析格式、Runtime、文件清单和 SHA-256，确认后再写入。</p></div>
                <span className="status-badge neutral">YAML / ZIP</span>
              </div>
              <div className="authoring-inspect-grid">
                <form className="authoring-input-card" onSubmit={e => { e.preventDefault(); inspectImport(); }}>
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
                  <button className="button accent" type="submit" disabled={inspectBusy}><Search size={16} /><span>{inspectBusy ? "检查中" : "只读检查"}</span></button>
                </form>
                <FormProvider {...importForm}>
                <form className="authoring-inspection-card" onSubmit={importForm.handleSubmit(commitImport)} noValidate>
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
                  <button className="button accent" type="submit" disabled={!importInspection || inspectBusy}><Check size={16} /><span>确认导入</span></button>
                </form>
                </FormProvider>
              </div>
              {inspectError && <div className="inline-alert error"><CircleAlert size={16} /><div><strong>操作失败</strong><p>{inspectError}</p></div></div>}
            </section>
          )}

          {!editingAgentId && mode === "project" && (
            <section className="authoring-mode-panel">
              <div className="authoring-panel-heading">
                <div><span className="eyebrow">Project detection</span><h2>识别现有项目</h2><p>复用 FrameworkDetector 展示证据和置信度；确认前不修改源码。</p></div>
                <span className="status-badge neutral">Workspace only</span>
              </div>
              <FormProvider {...projectForm}>
              <div className="authoring-inspect-grid">
                <form className="authoring-input-card" onSubmit={e => { e.preventDefault(); inspectProject(); }}>
                  <FormField label="工作区相对路径" requirement="required" htmlFor="projectPath" hint="仅检查当前工作区内的目录，不会修改项目源码。" error={projectForm.formState.errors.path?.message}>
                    <input id="projectPath" {...projectForm.register("path")} />
                  </FormField>
                  <button className="button accent" type="submit" disabled={inspectBusy}><Search size={16} /><span>{inspectBusy ? "检测中" : "检测项目"}</span></button>
                </form>
                <form className="authoring-inspection-card" onSubmit={projectForm.handleSubmit(commitProject)} noValidate>
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
                  <button className="button accent" type="submit" disabled={!projectInspection || inspectBusy}><Check size={16} /><span>确认创建 Revision</span></button>
                </form>
              </div>
              </FormProvider>
              {inspectError && <div className="inline-alert error"><CircleAlert size={16} /><div><strong>操作失败</strong><p>{inspectError}</p></div></div>}
            </section>
          )}

          {!editingAgentId && mode === "quick" && (
            <div className="wizard-layout">
              <FormProvider {...quickForm}>
              <form className="wizard-content" onSubmit={quickForm.handleSubmit(submitWizard)} noValidate>
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
                      options={RUNTIME_OPTIONS}
                      onValueChange={value => {
                        quickForm.setValue("runtimeType", value as QuickAgentFormValues["runtimeType"], { shouldDirty: true, shouldValidate: true });
                        markDirty();
                      }}
                    />
                  </FormField>
                  <FormField label="描述" requirement="optional" htmlFor="quickDescription" error={quickForm.formState.errors.description?.message}>
                    <input id="quickDescription" maxLength={1024} placeholder="简要说明这个 Agent 解决什么问题" {...quickForm.register("description", { onChange: markDirty })} />
                  </FormField>
                  <FormField label="系统提示词" requirement="required" htmlFor="quickPrompt" error={quickForm.formState.errors.prompt?.message}>
                    <div>
                    <textarea id="quickPrompt" rows={7} maxLength={32768} placeholder="例如：你是一名企业技术支持助手。先识别问题类型，再结合知识库给出准确、可执行的处理步骤；信息不足时先提问，不要编造事实。" {...quickForm.register("prompt", { onChange: markDirty })} />
                    <div className="field-footer"><span>写清角色、目标、工作边界和回答方式</span><span>{prompt.length} / 32768</span></div>
                    </div>
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
                  {runtime === "codex" && (
                    <div className="inline-alert warning codex-capability-notice">
                      <CircleAlert size={16} />
                      <div><strong>ManagedRuntime 不绑定 ksadk Tool</strong><p>codex CLI 自身提供工具能力，ksadk Tool 不会绑定到 codex Agent。MCP（streamable-http）与 Skill 可绑定：MCP 经 codex config_overrides 注入，Skill 以原生 SkillInput 注入。模型仍需选择并配置凭证。</p></div>
                    </div>
                  )}
                  <div className="capability-section">
                    <div className="capability-heading">
                      <span className="capability-icon"><Cpu size={15} /></span>
                      <div><h3>Model Profile</h3><p>负责理解请求、规划执行和生成回答；可多选</p></div>
                      <span className="required-badge">必需</span>
                    </div>
                    <div className="model-profile-control">
                      <button
                        className="button secondary"
                        type="button"
                        onClick={() => setConfigModel(selectedModelItems[0] || null)}
                      >
                        配置凭证
                      </button>
                    </div>
                    <StudioMultiSelect
                      ariaLabel="选择模型"
                      items={models}
                      selectedIds={selectedModels}
                      getId={item => item.resourceId}
                      getLabel={item => item.displayName}
                      getDescription={item => `${item.contract?.model || item.name} · ${credentialOf(item)?.configured ? "凭证已配置" : "需配置凭证"}`}
                      onChange={ids => { setSelectedModels(ids); markDirty(); }}
                      searchPlaceholder="搜索模型"
                      emptyMessage="没有可用模型"
                    />
                    <span className="helper">{selectedModelStatus}</span>
                  </div>
                  {runtime !== "codex" && (
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
                      getId={item => item.resourceId}
                      getLabel={item => item.displayName}
                      getDescription={item => `${item.description || "MCP Server"} · ${item.health?.toolCount || 0} Tool · ${item.status === "ready" ? "Ready" : item.status}`}
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
                    <button className="button secondary small" type="button" onClick={() => composeAgent({ preservePrompt: false })}>
                      <RefreshCw size={14} /><span>重新生成</span>
                    </button>
                  </div>
                  <div className="prompt-status">
                    <span className={`status-dot ${promptStatus === "done" ? "success" : "info"}`} />
                    <span>{promptStatus === "composing" ? "正在根据模板与能力生成 Agent 配置" : promptStatus === "done" ? "Agent 配置已根据当前选择生成" : "进入此步骤后生成 Prompt"}</span>
                  </div>
                  <FormField label="角色与系统提示词" requirement="required" htmlFor="composedSystemPrompt" hint="定义角色、目标、工作边界和回答原则" error={quickForm.formState.errors.systemPrompt?.message}>
                    <textarea id="composedSystemPrompt" className="prompt-editor" rows={16} {...quickForm.register("systemPrompt", { onChange: markDirty })} />
                  </FormField>
                  <FormField label="任务契约" requirement="optional" htmlFor="composedTaskPrompt" hint="约束每次请求的执行步骤、工具使用和交付结构" error={quickForm.formState.errors.taskPrompt?.message}>
                    <textarea id="composedTaskPrompt" className="prompt-editor" rows={10} {...quickForm.register("taskPrompt", { onChange: markDirty })} />
                  </FormField>
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
                      <div className="review-capability"><Cpu size={16} /><div><strong>{selectedModelItems[0]?.displayName || "Model"}</strong><span>Model Profile</span></div></div>
                      <div className="review-capability"><Wrench size={16} /><div><strong>{selectedTools.length} 个 Tool</strong><span>{policyMeta.title}</span></div></div>
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
                    <span><strong>创建后立即构建并打开会话</strong><small>生成不可变 AgentBundle，完成后进入 Chat 工作台</small></span>
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
                  <button className="button tertiary" type="button" onClick={saveDraft}>
                    <Copy size={16} /><span>保存草稿</span>
                  </button>
                  <span className="wizard-progress">第 {step} 步，共 4 步</span>
                  <button className="button tertiary" type="button" aria-expanded={summaryOpen} onClick={() => setSummaryOpen(v => !v)}>
                    <PanelRight size={16} /><span>配置摘要</span><span className="summary-count">{capCount}</span>
                  </button>
                  {step < 4 && (
                    <button className="button accent" type="button" onClick={() => gotoStep(step + 1)}>
                      <span>继续</span><ArrowRight size={16} />
                    </button>
                  )}
                  {step === 4 && (
                    <button className="button accent" type="submit" disabled={submitting}>
                      <Plus size={16} /><span>{submitting ? "正在创建" : "创建 Agent"}</span>
                    </button>
                  )}
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
                  <div><dt>Tool</dt><dd>{selectedTools.length}</dd></div>
                  <div><dt>策略</dt><dd>{template === "research" ? "Plan-Act-Observe" : "Direct"}</dd></div>
                </dl>
                <div className="summary-divider" />
                <div className="summary-note">
                  <ShieldCheck size={16} />
                  <div><strong>{policyMeta.title}</strong><p>{policyMeta.description}</p></div>
                </div>
                <div className="summary-note">
                  <Package size={16} />
                  <div><strong>不可变构建</strong><p>Skill、MCP 和 Tool 将锁定版本与摘要。</p></div>
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

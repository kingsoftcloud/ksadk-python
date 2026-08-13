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
import { quickAgentSchema, type QuickAgentFormValues } from "../schemas/agentForms";

export interface EditorCatalogItem {
  resourceId: string;
  kind: string;
  name: string;
  displayName: string;
  version: string;
  status: string;
  contract?: { model?: string };
  health?: { toolCount?: number };
}

interface AgentDetail {
  draft: {
    metadata: { id: string; name: string; revision: number; labels?: Record<string, string>; appearance?: AgentAppearance };
    spec: {
      description?: string;
      runtime?: { type?: string; projectPath?: string; entryPoint?: string; agentVariable?: string };
      instructions?: { system?: string; task?: string };
      execution?: { strategy?: string; maxSteps?: number; timeoutSeconds?: number };
      bindings?: {
        modelProfileId?: string | null;
        modelProfileIds?: string[];
        skills?: Array<{ resourceId: string; enabled?: boolean }>;
        mcpServers?: Array<{ resourceId: string; enabled?: boolean }>;
        [key: string]: unknown;
      };
      [key: string]: unknown;
    };
  };
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

function runtimeManifest(agentId: string, runtime: string) {
  if (runtime === "codex") return { type: "codex" };
  return {
    type: runtime,
    projectPath: ".",
    entryPoint: runtime === "adk" ? "agent.py" : "graph.py",
    agentVariable: runtime === "adk" ? "root_agent" : "app",
    agentId,
  };
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
  onSaved,
  onAppearanceSaved,
}: {
  agentId: string;
  catalog: EditorCatalogItem[];
  onSaved: (agentId: string, openChat: boolean) => void;
  onAppearanceSaved?: () => void;
}) {
  const [detail, setDetail] = useState<AgentDetail | null>(null);
  const [loadError, setLoadError] = useState("");
  const agentForm = useForm<QuickAgentFormValues>({
    resolver: zodResolver(quickAgentSchema) as Resolver<QuickAgentFormValues>,
    defaultValues: {
      name: "",
      slug: agentId,
      runtimeType: "codex",
      prompt: "",
      description: "",
    },
  });
  const { name, slug, runtimeType: runtime, prompt } = agentForm.watch();
  const [defaultModel, setDefaultModel] = useState("");
  const [selectedModels, setSelectedModels] = useState<string[]>([]);
  const [selectedSkills, setSelectedSkills] = useState<string[]>([]);
  const [selectedMcp, setSelectedMcp] = useState<string[]>([]);
  const [buildAfterSave, setBuildAfterSave] = useState(true);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState("");

  const models = useMemo(() => catalog.filter(item => item.kind === "model" && ["ready", "missing-secret"].includes(item.status)), [catalog]);
  const skills = useMemo(() => catalog.filter(item => item.kind === "skill" && item.status === "ready"), [catalog]);
  const mcps = useMemo(() => catalog.filter(item => item.kind === "mcp"), [catalog]);
  const selectedModelItems = selectedModels.map(id => models.find(item => item.resourceId === id)).filter(Boolean) as EditorCatalogItem[];

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
        agentForm.reset({
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
      })
      .catch(error => { if (active) setLoadError(error.message || "Agent 加载失败"); });
    return () => { active = false; };
  }, [agentId, agentForm]);

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
  const fallbackModel = detail?.draft.metadata.labels?.["agentkit.ksyun.com/model"] || "glm-5.1";
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
    "    projectPath: .",
    `    entryPoint: ${runtime === "adk" ? "agent.py" : "graph.py"}`,
    `    agentVariable: ${runtime === "adk" ? "root_agent" : "app"}`,
    "  instructions:",
    "    system: |-",
    ...prompt.split("\n").map(line => `      ${line}`),
  ].join("\n");

  async function save(values: QuickAgentFormValues) {
    if (!detail || saving) return;
    setSaving(true);
    setSaveError("");
    try {
      const original = detail.draft.spec;
      const spec = JSON.parse(JSON.stringify(original));
      spec.runtime = original.runtime || runtimeManifest(values.slug, values.runtimeType);
      spec.instructions = {
        system: values.prompt.trim(),
        task: original.instructions?.task || "",
      };
      spec.execution = {
        ...(original.execution || {}),
        strategy: original.execution?.strategy || "direct",
        maxSteps: original.execution?.maxSteps || 12,
        timeoutSeconds: original.execution?.timeoutSeconds || 120,
      };
      spec.bindings = {
        ...(original.bindings || {}),
        modelProfileId: defaultModel || null,
        modelProfileIds: selectedModels,
        skills: selectedSkills.map(resourceId => ({ resourceId, enabled: true })),
        mcpServers: selectedMcp.map(resourceId => ({ resourceId, enabled: true })),
      };
      const response = await apiFetch(`/api/v1/agents/${encodeURIComponent(agentId)}`, {
        method: "PUT",
        headers: {
          "Content-Type": "application/json",
          "If-Match": String(detail.draft.metadata.revision),
        },
        body: JSON.stringify(spec),
      });
      const saved = await response.json().catch(() => null);
      if (!response.ok) {
        if (applyApiFieldErrors(saved, agentForm.setError)) return;
        throw new Error(saved?.error?.message || `保存失败（${response.status}）`);
      }
      const savedId = saved?.metadata?.id || agentId;
      showToast("Agent 已更新", "agentengine.yaml 已回写，旧构建不会继续用于新会话。");

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
        showToast(`${values.runtimeType} Bundle 构建完成`, savedId);
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
      <form className="quick-create-form" onSubmit={agentForm.handleSubmit(save)} noValidate>
        <div className="quick-runtime-strip">
          <span className="runtime-logo"><Code size={17} /></span>
          <div><strong>{runtimeTitle(runtime)}</strong><span>一 Agent 一 YAML · 不可变 Bundle</span></div>
          <span className="status-badge success">本地可运行</span>
        </div>
        <div className="quick-create-heading">
          <span className="eyebrow">YAML-first</span>
          <h2>编辑 {slug}</h2>
          <p>保存会直接回写该 Agent 的 agentengine.yaml；旧构建会标记为过期。</p>
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
        <div className="form-grid two-columns">
          <FormField label="默认模型" requirement="required" htmlFor="editDefaultModel" hint="每轮未指定模型时使用">
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
            items={models}
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
          <div className="field-heading"><label>绑定 Skill / MCP</label><span className="helper">Skill 以原生 SkillInput 注入；MCP 经 codex config_overrides 注入</span></div>
          <div className="quick-capability-bindings">
            <StudioMultiSelect
              ariaLabel="选择绑定 Skill"
              items={skills}
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
              items={mcps}
              selectedIds={selectedMcp}
              getId={item => item.resourceId}
              getLabel={item => item.displayName}
              getDescription={item => `${item.version} · ${item.health?.toolCount || 0} Tool`}
              onChange={setSelectedMcp}
              searchPlaceholder="搜索 MCP"
              emptyMessage="没有已连接的 MCP"
            />
          </div>
        </div>
        <div className="quick-create-actions">
          <label className="checkbox-row">
            <input type="checkbox" checked={buildAfterSave} onChange={event => setBuildAfterSave(event.target.checked)} />
            <span><strong>保存后立即重新构建</strong><small>新构建完成后直接进入会话工作台</small></span>
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

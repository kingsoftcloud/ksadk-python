import { useCallback, useEffect, useRef, useState } from "react";
import { Check, RefreshCw } from "lucide-react";
import { zodResolver } from "@hookform/resolvers/zod";
import { useForm, type Resolver } from "react-hook-form";
import { z } from "zod";
import { InlineAlert } from "./Drawer";
import { showToast } from "./Toast";
import { apiFetch } from "../api";
import { FormField } from "./ui/FormField";
import { errorMessage } from "../pages/ResourcesPage";

/** 未填写接口地址时使用的默认 OpenAI 兼容端点（与 ksadk.model_proxy.config 一致）。 */
export const DEFAULT_MODEL_ENDPOINT_URL = "https://kspmas.ksyun.com/v1";

const modelSetupSchema = z.object({
  apiKey: z.string().max(16384, "API Key 不能超过 16384 个字符").default(""),
  endpointUrl: z.string().trim().max(1024, "接口地址不能超过 1024 个字符").default(""),
  manualModel: z.string().trim().max(256, "模型 ID 不能超过 256 个字符").default(""),
  temperature: z.coerce.number().min(0, "temperature 需在 0-2 之间").max(2, "temperature 需在 0-2 之间").default(0.2),
  maxTokens: z.coerce.number().int("max_tokens 需为 1-131072 的整数").min(1, "max_tokens 需为 1-131072 的整数").max(131072, "max_tokens 需为 1-131072 的整数").default(2048),
  credentialEnv: z.string().trim().regex(/^[A-Za-z_][A-Za-z0-9_]*$/, "环境变量名需为合法标识符").default("MODEL_API_KEY"),
});

type ModelSetupFormValues = z.infer<typeof modelSetupSchema>;

interface ProbeAttempt {
  protocol: string;
  endpointUrl: string;
  status: string;
  latencyMs?: number;
}

interface ProbeResult {
  attempts: ProbeAttempt[];
  models: string[];
  recommended: { protocol: string; wireApi: string; endpointUrl: string; status: string } | null;
}

/** 推荐默认模型的命中顺序；探测列表里第一个命中的作为预选项。 */
const PREFERRED_MODEL_HINTS = ["deepseek", "glm", "qwen", "kimi", "doubao", "claude", "gpt", "gemini"];

function pickDefaultModel(models: string[]): string {
  for (const hint of PREFERRED_MODEL_HINTS) {
    const hit = models.find(name => name.toLowerCase().includes(hint));
    if (hit) return hit;
  }
  return models[0] || "";
}

function modelSlug(modelId: string, taken: Set<string>): string {
  let slug = modelId.toLowerCase().replace(/[^a-z0-9._-]+/g, "-").replace(/^[-._]+/, "").slice(0, 120);
  if (!/^[a-z]/.test(slug)) slug = `model-${slug}`;
  if (slug.length < 2) slug = `${slug}-x`;
  let unique = slug;
  let counter = 2;
  while (taken.has(unique)) unique = `${slug}-${counter++}`;
  taken.add(unique);
  return unique;
}

const PROTOCOL_LABELS: Record<string, string> = { chat: "Chat", responses: "Responses", anthropic: "Anthropic" };
const STATUS_LABELS: Record<string, string> = {
  ok: "可用",
  auth_required: "需凭证",
  recognized: "可识别",
  unavailable: "不存在",
  unreachable: "不可达",
  error: "错误",
};

/** 设置页内嵌的极简添加模型表单：填 Key → 自动探测 → 勾选模型 → 创建。 */
export function ModelSetupForm({ onSaved }: { onSaved: () => void }) {
  const form = useForm<ModelSetupFormValues>({
    resolver: zodResolver(modelSetupSchema) as Resolver<ModelSetupFormValues>,
    defaultValues: {
      apiKey: "",
      endpointUrl: "",
      manualModel: "",
      temperature: 0.2,
      maxTokens: 2048,
      credentialEnv: "MODEL_API_KEY",
    },
  });
  const [probing, setProbing] = useState(false);
  const [probeError, setProbeError] = useState("");
  const [probe, setProbe] = useState<ProbeResult | null>(null);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  // 用户手动改过选择后，自动探测不再覆盖预选（用户手选优先）。
  const manualSelection = useRef(false);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState("");
  const [catalogModels, setCatalogModels] = useState<string[]>([]);

  useEffect(() => {
    apiFetch("/api/v1/catalog/models").then(r => r.json()).then(d => {
      setCatalogModels((d.items || []).map((i: any) => i.name).filter(Boolean));
    }).catch(() => {});
  }, []);

  const runProbe = useCallback(async () => {
    const values = form.getValues();
    const url = values.endpointUrl.trim() || DEFAULT_MODEL_ENDPOINT_URL;
    setProbing(true);
    setProbeError("");
    try {
      const body: any = { url };
      const apiKey = values.apiKey.trim();
      if (apiKey) body.apiKey = apiKey;
      const res = await apiFetch("/api/v1/model-endpoints:probe", {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
      });
      if (!res.ok) throw new Error(await errorMessage(res, "探测失败"));
      const data: ProbeResult = await res.json();
      setProbe(data);
      if (data.models.length > 0 && !manualSelection.current) {
        setSelected(new Set([pickDefaultModel(data.models)]));
      }
    } catch (e: any) {
      setProbe(null);
      setProbeError(e.message);
    }
    setProbing(false);
  }, [form]);

  // 填完 API Key 后自动智能探测；地址变化也重探（有 Key 才探，避免对空端点扫一遍）。
  const apiKeyValue = form.watch("apiKey");
  const endpointUrlValue = form.watch("endpointUrl");
  const probeTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(() => {
    if (apiKeyValue.trim().length < 8) return;
    if (probeTimer.current) clearTimeout(probeTimer.current);
    probeTimer.current = setTimeout(() => { void runProbe(); }, 700);
    return () => { if (probeTimer.current) clearTimeout(probeTimer.current); };
  }, [apiKeyValue, endpointUrlValue, runProbe]);

  function toggleModel(model: string) {
    manualSelection.current = true;
    setSelected(previous => {
      const next = new Set(previous);
      if (next.has(model)) next.delete(model);
      else next.add(model);
      return next;
    });
  }

  async function save() {
    const values = form.getValues();
    const envName = values.credentialEnv.trim() || "MODEL_API_KEY";
    const credentialRef = `env://${envName}`;
    const targets = selected.size > 0
      ? [...selected]
      : [values.manualModel.trim()].filter(Boolean);
    if (targets.length === 0) {
      setSaveError(probe?.models.length ? "请至少选择一个模型" : "请选择模型或手动填写模型 ID");
      return;
    }
    setSaving(true);
    setSaveError("");
    try {
      if (values.apiKey.trim()) {
        const credRes = await apiFetch(`/api/v1/credentials/${encodeURIComponent(envName)}`, {
          method: "PUT", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ value: values.apiKey.trim(), persistence: "session" }),
        });
        if (!credRes.ok) throw new Error(await errorMessage(credRes, "凭证保存失败"));
      }
      const endpointUrl = probe?.recommended?.endpointUrl
        || values.endpointUrl.trim()
        || DEFAULT_MODEL_ENDPOINT_URL;
      const wireApi = probe?.recommended?.wireApi || "";
      const takenNames = new Set<string>();
      for (const modelId of targets) {
        const spec: any = {
          provider: "openai-compatible",
          model: modelId,
          credentialRef,
          parameters: { temperature: values.temperature, max_tokens: values.maxTokens },
        };
        if (wireApi) spec.wireApi = wireApi;
        spec.endpointUrl = endpointUrl;
        const res = await apiFetch("/api/v1/catalog/model-profiles", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            name: modelSlug(modelId, takenNames),
            displayName: modelId,
            version: "1.0.0",
            description: "",
            spec,
          }),
        });
        if (!res.ok) {
          const payload = await res.json().catch(() => null);
          throw new Error(payload?.error?.message || `创建 ${modelId} 失败（${res.status}）`);
        }
      }
      showToast(targets.length > 1 ? "模型已创建" : "模型已创建", targets.join("、"));
      setProbe(null);
      setSelected(new Set());
      manualSelection.current = false;
      form.setValue("apiKey", "");
      form.setValue("manualModel", "");
      onSaved();
    } catch (e: any) {
      setSaveError(e.message);
    }
    setSaving(false);
  }

  const modelSuggestions = [...new Set([...(probe?.models || []), ...catalogModels])];
  const showModelPicker = (probe?.models.length || 0) > 0;
  const authNotice = probe?.recommended?.status === "auth_required" || probe?.attempts.some(a => a.status === "auth_required");

  return (
    <div className="model-setup-form" data-testid="model-setup-form">
      <FormField label="API Key" requirement="required" htmlFor="setupApiKey" hint="保存到当前工作区；填完后会自动智能探测可用模型。">
        <input
          id="setupApiKey"
          type="password"
          autoComplete="new-password"
          placeholder="输入模型服务 API Key"
          {...form.register("apiKey")}
        />
      </FormField>
      <FormField label="接口地址" requirement="optional" htmlFor="setupEndpoint" hint={`留空默认使用 ${DEFAULT_MODEL_ENDPOINT_URL}；支持主机、/v1 或完整 endpoint 地址，探测会自动归一化。`}>
        <input
          id="setupEndpoint"
          className="mono"
          placeholder={`默认 ${DEFAULT_MODEL_ENDPOINT_URL}`}
          {...form.register("endpointUrl")}
        />
      </FormField>

      {(probing || probe || probeError) && (
        <div className="model-probe-result" role="status">
          {probing && <span className="helper">正在智能探测可用协议与模型…</span>}
          {!probing && probe && (
            <>
              <div className="model-probe-attempts">
                {(probe.attempts || []).map((attempt, index) => (
                  <span
                    key={index}
                    className="badge"
                    data-state={attempt.status === "ok" ? "ready" : attempt.status === "unavailable" || attempt.status === "unreachable" ? "failed" : "pending"}
                    title={attempt.endpointUrl}
                  >
                    {PROTOCOL_LABELS[attempt.protocol] || attempt.protocol}
                    ·{STATUS_LABELS[attempt.status] || attempt.status}{attempt.latencyMs != null && attempt.status === "ok" ? ` ${attempt.latencyMs}ms` : ""}
                  </span>
                ))}
                {probe.recommended && (
                  <span className="tag">
                    推荐协议：{PROTOCOL_LABELS[probe.recommended.protocol] || probe.recommended.protocol}
                  </span>
                )}
              </div>
              {authNotice && <span className="helper">端点可达但需要有效凭证（401/403），确认 API Key 后可正常使用。</span>}
            </>
          )}
          {!probing && !probe && probeError && <span className="helper">探测未成功：{probeError}，可稍后重试或手动填写模型 ID。</span>}
        </div>
      )}

      {showModelPicker ? (
        <div className="form-field-scope">
          <span className="field-label">可用模型</span>
          <div className="model-chip-list">
            {probe!.models.map(model => (
              <button
                key={model}
                type="button"
                className="model-chip"
                data-selected={selected.has(model)}
                aria-pressed={selected.has(model)}
                onClick={() => toggleModel(model)}
              >
                {model}
              </button>
            ))}
          </div>
          <span className="helper">已预选推荐模型；点击可多选，保存时按所选批量创建。</span>
        </div>
      ) : (
        <FormField label="模型 ID" requirement="required" htmlFor="setupManualModel" hint="探测未返回模型清单时可手动填写。">
          <div>
            <input
              id="setupManualModel"
              className="mono"
              placeholder="deepseek-v4.1-flash / …"
              list="setup-model-suggestions"
              {...form.register("manualModel")}
            />
            <datalist id="setup-model-suggestions">{modelSuggestions.map(m => <option key={m} value={m} />)}</datalist>
          </div>
        </FormField>
      )}

      <details className="setup-advanced">
        <summary>高级选项</summary>
        <div className="form-grid two-columns">
          <FormField label="temperature" requirement="optional" htmlFor="setupTemp" error={form.formState.errors.temperature?.message}>
            <input id="setupTemp" type="number" step="0.1" min={0} max={2} {...form.register("temperature", { valueAsNumber: true })} />
          </FormField>
          <FormField label="max_tokens" requirement="optional" htmlFor="setupMaxTokens" error={form.formState.errors.maxTokens?.message}>
            <input id="setupMaxTokens" type="number" min={1} max={131072} {...form.register("maxTokens", { valueAsNumber: true })} />
          </FormField>
        </div>
        <FormField label="凭证环境变量名" requirement="required" htmlFor="setupCredentialEnv" error={form.formState.errors.credentialEnv?.message}>
          <input id="setupCredentialEnv" className="mono" placeholder="MODEL_API_KEY" {...form.register("credentialEnv")} />
        </FormField>
      </details>

      {saveError && <InlineAlert kind="error" title="添加模型" message={saveError} />}

      <div className="model-setup-actions">
        <button
          className="button secondary"
          type="button"
          onClick={() => { manualSelection.current = false; void runProbe(); }}
          disabled={probing}
        >
          <RefreshCw size={14} /><span>{probing ? "探测中…" : "重新探测"}</span>
        </button>
        <button className="button accent" type="button" onClick={save} disabled={saving}>
          <Check size={15} /><span>{saving ? "创建中…" : `创建模型${selected.size > 1 ? `（${selected.size} 个）` : ""}`}</span>
        </button>
      </div>
    </div>
  );
}

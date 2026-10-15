import { useCallback, useEffect, useState } from "react";
import { Check, Monitor, Moon, Sun } from "lucide-react";
import { zodResolver } from "@hookform/resolvers/zod";
import { FormProvider, useForm, type Resolver } from "react-hook-form";
import { showToast } from "../components/Toast";
import { ModelCredentialDrawer, type ResItem } from "./ResourcesPage";
import { ModelSetupForm } from "../components/ModelSetupForm";
import { apiFetch } from "../api";
import type { StudioThemePreference } from "../studioTheme";
import { FormField } from "../components/ui/FormField";
import { StudioSelect } from "../components/ui/StudioSelect";
import { applyApiFieldErrors } from "../lib/formErrors";
import { settingsSchema, type SettingsFormValues } from "../schemas/resourceForms";

const APPEARANCE_OPTIONS: Array<{
  value: StudioThemePreference;
  label: string;
  description: string;
  icon: typeof Monitor;
}> = [
  { value: "system", label: "跟随系统", description: "随 macOS 或浏览器切换", icon: Monitor },
  { value: "light", label: "浅色", description: "始终使用明亮界面", icon: Sun },
  { value: "dark", label: "深色", description: "始终使用暗色界面", icon: Moon },
];

export type SettingsSection = "models" | "general" | "cloud" | "runtime" | "about";

export const SETTINGS_SECTION_IDS: SettingsSection[] = ["models", "general", "cloud", "runtime", "about"];

const SETTINGS_SECTIONS: Array<{ id: SettingsSection; label: string }> = [
  { id: "models", label: "模型" },
  { id: "general", label: "通用" },
  { id: "cloud", label: "云端连接" },
  { id: "runtime", label: "运行与沙箱" },
  { id: "about", label: "关于" },
];

function credentialSourceLabel(source: string): string {
  source = source.replace(/-alias$/, "");
  if (source === "workspace" || source === "session") return "工作区";
  if (source === "environment") return "启动环境";
  if (source === "env-file") return "启动时指定的配置";
  if (source === "dotenv") return "项目环境配置";
  return source === "missing" ? "未配置" : source;
}

export function normalizeSandbox(value?: string): SettingsFormValues["sandbox"] {
  const normalized = (value || "workspace-write-auto").replaceAll("_", "-");
  if (
    normalized === "read-only"
    || normalized === "workspace-write"
    || normalized === "workspace-write-auto"
    || normalized === "full-access"
  ) return normalized;
  return "workspace-write-auto";
}

/** G7 UI：Harness 沙箱档位归一化（隔离强度语义，后端自动按平台定）。 */
export function normalizeHarnessSandbox(value?: string): SettingsFormValues["harnessSandbox"] {
  const normalized = (value || "workspace-write-local").replaceAll("_", "-");
  if (
    normalized === "read-only"
    || normalized === "workspace-write-local"
    || normalized === "workspace-write-remote"
    || normalized === "full-access"
  ) return normalized;
  return "workspace-write-local";
}

interface ModelRow {
  item: ResItem;
  ref: string;
  name: string;
  configured: boolean;
  source: string;
}

/** 整页嵌入式设置：左侧分类栏 + 右侧内容区（对标系统设置样式）。 */
export function SettingsPage({ themePreference, onThemePreferenceChange, initialSection = "models", onSectionChange }: {
  themePreference: StudioThemePreference;
  onThemePreferenceChange: (preference: StudioThemePreference) => void;
  initialSection?: SettingsSection;
  onSectionChange?: (section: SettingsSection) => void;
}) {
  const [settings, setSettings] = useState<any>(null);
  const settingsForm = useForm<SettingsFormValues>({
    resolver: zodResolver(settingsSchema) as Resolver<SettingsFormValues>,
    defaultValues: {
      sandbox: "workspace-write-auto",
      harnessSandbox: "workspace-write-local",
      buildAfterCreate: true,
      codexProxy: "auto",
      cloudRegion: "",
      cloudBucket: "",
      cloudServerUrl: "",
      cloudAccessKey: "",
      cloudSecretKey: "",
      cloudAccountId: "",
    },
  });
  const [about, setAbout] = useState<Array<[string, string]>>([]);
  const [saving, setSaving] = useState(false);
  const [configModel, setConfigModel] = useState<ResItem | null>(null);
  const [activeSection, setActiveSection] = useState<SettingsSection>(initialSection);
  const [modelRows, setModelRows] = useState<ModelRow[]>([]);
  const [updateState, setUpdateState] = useState<{ latestVersion: string | null; hasUpdate: boolean; error?: string } | null>(null);
  const [checkingUpdate, setCheckingUpdate] = useState(false);

  useEffect(() => { setActiveSection(initialSection); }, [initialSection]);

  function switchSection(section: SettingsSection) {
    setActiveSection(section);
    onSectionChange?.(section);
  }

  function revealField(name: keyof SettingsFormValues) {
    switchSection(name.startsWith("cloud") ? "cloud" : "runtime");
    requestAnimationFrame(() => settingsForm.setFocus(name));
  }

  async function checkUpdate() {
    setCheckingUpdate(true);
    try {
      const res = await apiFetch("/api/v1/system/update-check");
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      setUpdateState(await res.json());
    } catch {
      setUpdateState({ latestVersion: null, hasUpdate: false, error: "network" });
    }
    setCheckingUpdate(false);
  }

  const loadModels = useCallback(async () => {
    try {
      const resources = await apiFetch("/api/v1/catalog/resources?limit=200").then(r => r.json());
      const items: ResItem[] = (resources.items || []).filter(
        (i: ResItem) => i.kind === "model" && (i.source === "local" || i.source === "market"),
      );
      const rows: ModelRow[] = [];
      for (const item of items) {
        const ref = item.requiredSecretRefs?.[0] || item.contract?.credentialRef || "";
        const name = ref.replace(/^env:\/\//, "");
        let status: any = { configured: false, source: "missing" };
        if (name) {
          try { status = await apiFetch(`/api/v1/credentials/${encodeURIComponent(name)}`).then(r => r.json()); } catch {}
        }
        rows.push({ item, ref, name, configured: Boolean(status?.configured), source: status?.source || "missing" });
      }
      setModelRows(rows);
    } catch {
      setModelRows([]);
    }
  }, []);

  useEffect(() => {
    (async () => {
      try {
        const s = await apiFetch("/api/v1/system/settings").then(r => r.json());
        setSettings(s);
        settingsForm.reset({
          sandbox: normalizeSandbox(s.sandbox),
          harnessSandbox: normalizeHarnessSandbox(s.harnessSandbox),
          buildAfterCreate: s.buildAfterCreate !== false,
          codexProxy: s.codexProxy || "auto",
          cloudRegion: s.cloudRegion || "",
          cloudBucket: s.cloudBucket || "",
          cloudServerUrl: s.cloudServerUrl || "",
          cloudAccessKey: "",
          cloudSecretKey: "",
          cloudAccountId: s.cloudAccountId || "",
        });
      } catch { setSettings({}); }
      await loadModels();
      try {
        const b = await apiFetch("/api/v1/system/bootstrap").then(r => r.json());
        setAbout([
          ["工作区", b?.workspace?.name || "-"],
          ["路径", b?.workspace?.path || "-"],
          ["API 版本", b?.apiVersion || "-"],
          ["Studio 版本", b?.sdkVersion || "-"],
        ]);
      } catch { setAbout([]); }
    })();
  }, [loadModels, settingsForm]);

  async function save(values: SettingsFormValues) {
    setSaving(true);
    try {
      const payload: any = {
        sandbox: values.sandbox,
        harnessSandbox: values.harnessSandbox,
        buildAfterCreate: values.buildAfterCreate,
        codexProxy: values.codexProxy,
      };
      if (values.cloudRegion.trim()) payload.cloudRegion = values.cloudRegion.trim();
      if (values.cloudBucket.trim()) payload.cloudBucket = values.cloudBucket.trim();
      if (values.cloudServerUrl.trim()) payload.cloudServerUrl = values.cloudServerUrl.trim();
      // 云账号:留空 = 不修改;AccountID 空串不提交(避免清掉已存值)
      if (values.cloudAccessKey.trim()) payload.cloudAccessKey = values.cloudAccessKey.trim();
      if (values.cloudSecretKey.trim()) payload.cloudSecretKey = values.cloudSecretKey.trim();
      // Do not carry an untouched old account ID into a newly entered key pair.
      if (values.cloudAccountId.trim() && (!payload.cloudAccessKey || settingsForm.formState.dirtyFields.cloudAccountId)) {
        payload.cloudAccountId = values.cloudAccountId.trim();
      }
      const res = await apiFetch("/api/v1/system/settings", {
        method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload),
      });
      if (!res.ok) {
        const errorPayload = await res.json().catch(() => null);
        if (applyApiFieldErrors(errorPayload, (name, error) => {
          if (!Object.hasOwn(settingsForm.getValues(), name)) {
            showToast("保存失败", error.message || "请检查配置", "error");
            return;
          }
          const field = name as keyof SettingsFormValues;
          settingsForm.setError(field, error);
          revealField(field);
        })) {
          setSaving(false);
          return;
        }
        throw new Error(errorPayload?.error?.message || `保存失败（${res.status}）`);
      }
      showToast("设置已保存", "已保存到当前工作区，运行配置已更新。");
    } catch (e: any) {
      showToast("保存失败", e.message, "error");
    }
    setSaving(false);
  }

  const settingsSaveButton = (
    <button className="button accent" type="button" onClick={settingsForm.handleSubmit(save, errors => {
      const name = Object.keys(errors)[0] as keyof SettingsFormValues;
      if (name) revealField(name);
    })} disabled={saving || settings == null}>
      <Check size={15} /><span>{saving ? "保存中" : "保存"}</span>
    </button>
  );

  return (
    <FormProvider {...settingsForm}>
    <div className="page-container settings-page">
      <div className="settings-page-layout">
      <nav className="settings-page-nav" aria-label="设置分类">
        {SETTINGS_SECTIONS.map(section => (
          <button
            key={section.id}
            className={activeSection === section.id ? "active" : ""}
            type="button"
            aria-pressed={activeSection === section.id}
            aria-controls={`settings-${section.id}`}
            onClick={() => switchSection(section.id)}
          >
            {section.label}
          </button>
        ))}
      </nav>
      <div className="settings-page-sections">
      <section id="settings-models" hidden={activeSection !== "models"} className="settings-group" tabIndex={-1}>
        <h3>模型</h3>
        {modelRows.length === 0 ? (
          <div className="settings-empty">还没有模型配置。填写 API Key 即可自动探测并创建。</div>
        ) : modelRows.map(row => (
          <ModelRowLine key={row.item.resourceId} row={row} onDeleted={loadModels} onConfigure={() => setConfigModel(row.item)} />
        ))}
        <h3 className="settings-model-add-title">添加模型</h3>
        <ModelSetupForm onSaved={loadModels} />
      </section>

      <section id="settings-general" hidden={activeSection !== "general"} className="settings-group" tabIndex={-1}>
        <h3>外观</h3>
        <div className="appearance-options" role="radiogroup" aria-label="颜色模式">
          {APPEARANCE_OPTIONS.map(option => {
            const Icon = option.icon;
            return (
              <label key={option.value} className={`appearance-option${themePreference === option.value ? " selected" : ""}`}>
                <Icon aria-hidden="true" />
                <span><strong>{option.label}</strong></span>
                <input
                  type="radio"
                  name="studio-theme"
                  value={option.value}
                  checked={themePreference === option.value}
                  onChange={() => onThemePreferenceChange(option.value)}
                />
              </label>
            );
          })}
        </div>
        <p className="appearance-note">立即生效，仅保存在当前浏览器。</p>
      </section>

      <section id="settings-cloud" hidden={activeSection !== "cloud"} className="settings-group" tabIndex={-1}>
        <h3>云端部署</h3>
        <p className="helper">配置本地平台资源调用和云端部署使用的连接，保存后无需另建环境文件。</p>
        <FormField label="控制面地址" requirement="optional" htmlFor="settingCloudServerUrl" hint="平台资源调用使用的 HTTPS 服务地址；留空保留现有配置。" error={settingsForm.formState.errors.cloudServerUrl?.message}>
          <input id="settingCloudServerUrl" placeholder="https://…" {...settingsForm.register("cloudServerUrl")} />
        </FormField>
        <p className="helper">地址来源：{credentialSourceLabel(settings?.configurationSources?.cloudServerUrl || "missing")}；账号来源：{credentialSourceLabel(settings?.configurationSources?.cloudAccessKey || "missing")}</p>
        <div className="form-grid two-columns">
          <FormField label="Region" requirement="optional" htmlFor="settingCloudRegion" error={settingsForm.formState.errors.cloudRegion?.message}>
            <input id="settingCloudRegion" placeholder="cn-beijing-6" {...settingsForm.register("cloudRegion")} />
          </FormField>
          <FormField label="KS3 Bucket" requirement="optional" htmlFor="settingCloudBucket" hint="留空时复用启动环境或 SDK 默认 Bucket。" error={settingsForm.formState.errors.cloudBucket?.message}>
            <input id="settingCloudBucket" placeholder="agentengine-<account>-cn-beijing-6" {...settingsForm.register("cloudBucket")} />
          </FormField>
        </div>
        <div className="form-grid two-columns">
          <FormField label="Access Key" requirement="optional" htmlFor="settingCloudAccessKey" hint="金山云账号 AK，用于云端请求签名；留空保留已保存值。" error={settingsForm.formState.errors.cloudAccessKey?.message}>
            <input id="settingCloudAccessKey" type="password" autoComplete="off" placeholder={settings?.cloudAccountConfigured ? "已配置（留空保持不变）" : "AKLT..."} {...settingsForm.register("cloudAccessKey")} />
          </FormField>
          <FormField label="Secret Key" requirement="optional" htmlFor="settingCloudSecretKey" hint="金山云账号 SK；留空保留已保存值。" error={settingsForm.formState.errors.cloudSecretKey?.message}>
            <input id="settingCloudSecretKey" type="password" autoComplete="off" placeholder={settings?.cloudAccountConfigured ? "已配置（留空保持不变）" : ""} {...settingsForm.register("cloudSecretKey")} />
          </FormField>
        </div>
        <FormField label="Account ID" requirement="optional" htmlFor="settingCloudAccountId" hint="主账号 ID（X-Ksc-Account-Id）；可从金山云控制台获取。" error={settingsForm.formState.errors.cloudAccountId?.message}>
          <input id="settingCloudAccountId" placeholder="10203040..." {...settingsForm.register("cloudAccountId")} />
        </FormField>
        {!!settings?.platformResourceMissingFields?.length && <p className="helper" role="status">平台资源连接还需配置：{settings.platformResourceMissingFields.map((key: string) => ({cloudAccessKey: "Access Key", cloudSecretKey: "Secret Key", cloudServerUrl: "控制面地址"}[key] || key)).join("、")}</p>}
        <p className="helper">平台资源：{settings?.platformResourcesConfigured ? "连接配置完整" : "连接配置未完成"}；云端账号：{settings?.cloudAccountConfigured ? "已就绪" : "尚未配置"}；云端部署：{settings?.cloudSignedAccountConfigured ? "已就绪" : "尚未配置"}</p>
        <div className="settings-section-actions">{settingsSaveButton}</div>
      </section>

      <section id="settings-runtime" hidden={activeSection !== "runtime"} className="settings-group" tabIndex={-1}>
        <h3>执行与沙箱</h3>
        <FormField label="默认执行权限（Codex）" requirement="required" htmlFor="settingSandbox" hint="新 Agent 默认值；会话页可单次覆盖，下一轮对话生效。" error={settingsForm.formState.errors.sandbox?.message}>
          <StudioSelect
            id="settingSandbox"
            ariaLabel="默认执行权限"
            value={settingsForm.watch("sandbox")}
            options={[
              { value: "read-only", label: "只读沙箱（不可写）" },
              { value: "workspace-write", label: "请求批准（写工作区，每次询问）" },
              { value: "workspace-write-auto", label: "风险操作需确认（写工作区）" },
              { value: "full-access", label: "完全访问（不受限读写）" },
            ]}
            onValueChange={value => settingsForm.setValue("sandbox", value as SettingsFormValues["sandbox"], { shouldDirty: true, shouldValidate: true })}
          />
        </FormField>
        <FormField label="默认沙箱（Harness 通用智能体）" requirement="required" htmlFor="settingHarnessSandbox" hint="隔离强度语义；后端按部署形态自动选择。会话页可单次覆盖。" error={settingsForm.formState.errors.harnessSandbox?.message}>
          <StudioSelect
            id="settingHarnessSandbox"
            ariaLabel="默认沙箱（Harness）"
            value={settingsForm.watch("harnessSandbox")}
            options={[
              { value: "read-only", label: "只读沙箱（不可写）" },
              { value: "workspace-write-local", label: "工作区可写 · 本地隔离" },
              { value: "workspace-write-remote", label: "工作区可写 · 远程沙箱" },
              { value: "full-access", label: "完全访问（不受限读写）" },
            ]}
            onValueChange={value => settingsForm.setValue("harnessSandbox", value as SettingsFormValues["harnessSandbox"], { shouldDirty: true, shouldValidate: true })}
          />
        </FormField>
        <div className="studio-form-field">
          <label className="checkbox-row">
            <input type="checkbox" {...settingsForm.register("buildAfterCreate")} />
            <span><strong>创建后立即构建</strong><small>新建 Agent 保存后自动构建并进入会话</small></span>
          </label>
        </div>
        <h3>运行时</h3>
        <FormField label="Codex Responses→Chat 代理" requirement="required" htmlFor="settingCodexProxy" hint="非原生 Responses 上游可启用兼容代理。" error={settingsForm.formState.errors.codexProxy?.message}>
          <StudioSelect
            id="settingCodexProxy"
            ariaLabel="Codex Responses 代理"
            value={settingsForm.watch("codexProxy")}
            options={[
              { value: "auto", label: "自动（探测）" },
              { value: "forced", label: "强制启用" },
              { value: "direct", label: "强制直连" },
            ]}
            onValueChange={value => settingsForm.setValue("codexProxy", value as SettingsFormValues["codexProxy"], { shouldDirty: true, shouldValidate: true })}
          />
        </FormField>
        <div className="settings-section-actions">{settingsSaveButton}</div>
      </section>

      <section id="settings-about" hidden={activeSection !== "about"} className="settings-group" tabIndex={-1}>
        <h3>关于</h3>
        <dl className="trace-detail-grid">
          {about.map(([k, v]) => <div key={k}><dt>{k}</dt><dd>{v}</dd></div>)}
        </dl>
        <div className="settings-update-check">
          <button className="button secondary small" type="button" onClick={checkUpdate} disabled={checkingUpdate}>
            {checkingUpdate ? "检查中…" : "检查更新"}
          </button>
          {updateState && (
            <span className="helper" role="status">
              {updateState.error
                ? "检查失败，请确认网络后重试。"
                : updateState.hasUpdate
                  ? `发现新版本 ${updateState.latestVersion}，可运行 pip install -U ksadk 升级。`
                  : `已是最新版本。`}
            </span>
          )}
        </div>
      </section>
      </div>
      </div>

      {configModel && (
        <ModelCredentialDrawer
          model={configModel}
          onClose={() => setConfigModel(null)}
          onChanged={loadModels}
        />
      )}
    </div>
    </FormProvider>
  );
}

function ModelRowLine({ row, onDeleted, onConfigure }: {
  row: ModelRow;
  onDeleted: () => void;
  onConfigure: () => void;
}) {
  const [confirming, setConfirming] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const endpoint = row.item.contract?.endpointUrl || row.item.contract?.baseUrl || "";
  const modelId = row.item.contract?.model || "";

  async function remove() {
    setDeleting(true);
    try {
      const res = await apiFetch(`/api/v1/catalog/resources/${encodeURIComponent(row.item.resourceId)}`, { method: "DELETE" });
      if (!res.ok) throw new Error(`删除失败（${res.status}）`);
      showToast("模型已删除", row.item.displayName);
      onDeleted();
    } catch (e: any) {
      showToast("删除失败", e.message, "error");
    }
    setDeleting(false);
    setConfirming(false);
  }

  return (
    <div className="settings-credential">
      <span>
        <strong>{row.item.displayName}</strong>
        <small>
          {modelId}{endpoint ? ` · ${endpoint}` : ""} · {row.configured ? `凭证已配置 · ${credentialSourceLabel(row.source)}` : "凭证未配置"}
        </small>
      </span>
      <span className="row-actions">
        <button className="button secondary small" type="button" onClick={onConfigure}>配置凭证</button>
        {row.item.source === "local" && (
          <button
            className="button secondary small"
            type="button"
            data-state={confirming ? "danger" : undefined}
            onClick={() => (confirming ? remove() : setConfirming(true))}
            onBlur={() => setConfirming(false)}
            disabled={deleting}
          >
            {deleting ? "删除中…" : confirming ? "确认删除" : "删除"}
          </button>
        )}
      </span>
    </div>
  );
}

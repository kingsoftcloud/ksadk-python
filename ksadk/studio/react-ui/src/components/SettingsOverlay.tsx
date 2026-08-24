import { useCallback, useEffect, useState } from "react";
import { Check, Monitor, Moon, Sun } from "lucide-react";
import { zodResolver } from "@hookform/resolvers/zod";
import { FormProvider, useForm, type Resolver } from "react-hook-form";
import { Drawer } from "./Drawer";
import { showToast } from "./Toast";
import { ModelCredentialDrawer, type ResItem } from "../pages/ResourcesPage";
import { apiFetch } from "../api";
import type { StudioThemePreference } from "../studioTheme";
import { FormField } from "./ui/FormField";
import { StudioSelect } from "./ui/StudioSelect";
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

export type SettingsSection = "general" | "credentials" | "cloud" | "runtime" | "about";

const SETTINGS_SECTIONS: Array<{ id: SettingsSection; label: string }> = [
  { id: "general", label: "通用" },
  { id: "credentials", label: "模型与凭证" },
  { id: "cloud", label: "云端连接" },
  { id: "runtime", label: "运行与沙箱" },
  { id: "about", label: "关于" },
];

function credentialSourceLabel(source: string): string {
  if (source === "workspace" || source === "session") return "工作区";
  if (source === "environment") return "启动环境";
  return source === "missing" ? "未配置" : source;
}

export function normalizeSandbox(value?: string): SettingsFormValues["sandbox"] {
  const normalized = (value || "read-only").replaceAll("_", "-");
  if (
    normalized === "workspace-write"
    || normalized === "workspace-write-auto"
    || normalized === "full-access"
  ) return normalized;
  return "read-only";
}

/** 工作区级设置抽屉。 */
export function SettingsOverlay({ themePreference, onThemePreferenceChange, initialSection = "general", onClose }: {
  themePreference: StudioThemePreference;
  onThemePreferenceChange: (preference: StudioThemePreference) => void;
  initialSection?: SettingsSection;
  onClose: () => void;
}) {
  const [settings, setSettings] = useState<any>(null);
  const settingsForm = useForm<SettingsFormValues>({
    resolver: zodResolver(settingsSchema) as Resolver<SettingsFormValues>,
    defaultValues: {
      sandbox: "read-only",
      buildAfterCreate: true,
      codexProxy: "auto",
      cloudRegion: "",
      cloudBucket: "",
    },
  });
  const [credRows, setCredRows] = useState<Array<{ ref: string; name: string; configured: boolean; source: string; model: ResItem }>>([]);
  const [about, setAbout] = useState<Array<[string, string]>>([]);
  const [saving, setSaving] = useState(false);
  const [configModel, setConfigModel] = useState<ResItem | null>(null);
  const [activeSection, setActiveSection] = useState<SettingsSection>(initialSection);

  const scrollToSection = useCallback((section: SettingsSection) => {
    setActiveSection(section);
    document.getElementById(`settings-${section}`)?.scrollIntoView({ behavior: "smooth", block: "start" });
  }, []);

  useEffect(() => {
    const frame = requestAnimationFrame(() => scrollToSection(initialSection));
    return () => cancelAnimationFrame(frame);
  }, [initialSection, scrollToSection]);

  const loadCredentials = useCallback(async () => {
    try {
      const [resources, discovered] = await Promise.all([
        apiFetch("/api/v1/catalog/resources?limit=200").then(r => r.json()),
        apiFetch("/api/v1/catalog/models").then(r => r.json()).catch(() => null),
      ]);
      let items: ResItem[] = (resources.items || []).filter((i: ResItem) => i.kind === "model");
      if (discovered?.items?.length) {
        items = [...items.filter(i => i.source === "local" || i.source === "market"), ...discovered.items];
      }
      const rows: Array<{ ref: string; name: string; configured: boolean; source: string; model: ResItem }> = [];
      const seen = new Set<string>();
      for (const model of items) {
        const ref = model.requiredSecretRefs?.[0] || model.contract?.credentialRef || "";
        if (!ref || seen.has(ref)) continue;
        seen.add(ref);
        const name = ref.replace(/^env:\/\//, "");
        let status: any = { configured: false, source: "missing" };
        try { status = await apiFetch(`/api/v1/credentials/${encodeURIComponent(name)}`).then(r => r.json()); } catch {}
        rows.push({ ref, name, configured: Boolean(status?.configured), source: status?.source || "missing", model });
      }
      setCredRows(rows);
    } catch { setCredRows([]); }
  }, []);

  useEffect(() => {
    (async () => {
      try {
        const s = await apiFetch("/api/v1/system/settings").then(r => r.json());
        setSettings(s);
        settingsForm.reset({
          sandbox: normalizeSandbox(s.sandbox),
          buildAfterCreate: s.buildAfterCreate !== false,
          codexProxy: s.codexProxy || "auto",
          cloudRegion: s.cloudRegion || "",
          cloudBucket: s.cloudBucket || "",
        });
      } catch { setSettings({}); }
      await loadCredentials();
      try {
        const b = await apiFetch("/api/v1/system/bootstrap").then(r => r.json());
        setAbout([
          ["工作区", b?.workspace?.name || "-"],
          ["路径", b?.workspace?.path || "-"],
          ["API 版本", b?.apiVersion || "-"],
        ]);
      } catch { setAbout([]); }
    })();
  }, [loadCredentials, settingsForm]);

  async function save(values: SettingsFormValues) {
    setSaving(true);
    try {
      const payload: any = {
        sandbox: values.sandbox,
        buildAfterCreate: values.buildAfterCreate,
        codexProxy: values.codexProxy,
      };
      if (values.cloudRegion.trim()) payload.cloudRegion = values.cloudRegion.trim();
      if (values.cloudBucket.trim()) payload.cloudBucket = values.cloudBucket.trim();
      const res = await apiFetch("/api/v1/system/settings", {
        method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload),
      });
      if (!res.ok) {
        const errorPayload = await res.json().catch(() => null);
        if (applyApiFieldErrors(errorPayload, settingsForm.setError)) {
          setSaving(false);
          return;
        }
        throw new Error(errorPayload?.error?.message || `保存失败（${res.status}）`);
      }
      onClose();
      showToast("设置已保存", "工作区级配置已写入 .agentkit/settings.yaml。");
    } catch (e: any) {
      showToast("保存失败", e.message, "error");
    }
    setSaving(false);
  }

  return (
    <FormProvider {...settingsForm}>
    <Drawer
      title="设置"
      subtitle="工作区级配置，保存到 .agentkit/settings.yaml，重启后仍生效。"
      wide
      onClose={onClose}
      footer={
        <>
          <button className="button secondary" type="button" onClick={onClose}>取消</button>
          <button className="button accent" type="button" onClick={settingsForm.handleSubmit(save)} disabled={saving || settings == null}>
            <Check size={15} /><span>{saving ? "保存中" : "保存"}</span>
          </button>
        </>
      }
    >
      <div className="settings-layout">
      <nav className="settings-section-nav" aria-label="设置分类">
        {SETTINGS_SECTIONS.map(section => (
          <button
            key={section.id}
            className={activeSection === section.id ? "active" : ""}
            type="button"
            onClick={() => scrollToSection(section.id)}
          >
            {section.label}
          </button>
        ))}
      </nav>
      <div className="settings-sections">
      <section id="settings-general" className="settings-group" tabIndex={-1}>
        <h3>外观</h3>
        <div className="appearance-options" role="radiogroup" aria-label="颜色模式">
          {APPEARANCE_OPTIONS.map(option => {
            const Icon = option.icon;
            return (
              <label key={option.value} className={`appearance-option${themePreference === option.value ? " selected" : ""}`}>
                <Icon aria-hidden="true" />
                <span><strong>{option.label}</strong><small>{option.description}</small></span>
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
        <p className="appearance-note">外观仅保存到当前浏览器，并会立即应用到 Studio 与会话工作台。</p>
      </section>

      <section id="settings-runtime" className="settings-group" tabIndex={-1}>
        <h3>执行与沙箱</h3>
        <FormField label="默认执行权限（Codex）" requirement="required" htmlFor="settingSandbox" hint="新 Agent 默认值；会话页可单次覆盖，下一轮对话生效。" error={settingsForm.formState.errors.sandbox?.message}>
          <StudioSelect
            id="settingSandbox"
            ariaLabel="默认执行权限"
            value={settingsForm.watch("sandbox")}
            options={[
              { value: "read-only", label: "只读沙箱（不可写）" },
              { value: "workspace-write", label: "请求批准（写工作区，每次询问）" },
              { value: "workspace-write-auto", label: "替我审批（写工作区，仅风险询问）" },
              { value: "full-access", label: "完全访问（不受限读写）" },
            ]}
            onValueChange={value => settingsForm.setValue("sandbox", value as SettingsFormValues["sandbox"], { shouldDirty: true, shouldValidate: true })}
          />
        </FormField>
        <div className="studio-form-field">
          <label className="checkbox-row">
            <input type="checkbox" {...settingsForm.register("buildAfterCreate")} />
            <span><strong>创建后立即构建</strong><small>新建 Agent 保存后自动构建并进入会话</small></span>
          </label>
        </div>
      </section>

      <section id="settings-credentials" className="settings-group" tabIndex={-1}>
        <h3>凭证</h3>
        {credRows.length === 0 ? (
          <div className="settings-empty">暂无凭证</div>
        ) : credRows.map(row => (
          <div key={row.ref} className="settings-credential">
            <span>
              <strong>{row.name}</strong>
              <small>{row.configured ? `已配置 · ${credentialSourceLabel(row.source)}` : "未配置"}</small>
            </span>
            <button className="button secondary small" type="button" onClick={() => setConfigModel(row.model)}>配置</button>
          </div>
        ))}
      </section>

      <section id="settings-runtime-proxy" className="settings-group" tabIndex={-1}>
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
      </section>

      <section id="settings-cloud" className="settings-group" tabIndex={-1}>
        <h3>云端部署</h3>
        <p className="helper">配置云端部署使用的区域和制品存储。</p>
        <div className="form-grid two-columns">
          <FormField label="Region" requirement="optional" htmlFor="settingCloudRegion" error={settingsForm.formState.errors.cloudRegion?.message}>
            <input id="settingCloudRegion" placeholder="cn-beijing-6" {...settingsForm.register("cloudRegion")} />
          </FormField>
          <FormField label="KS3 Bucket" requirement="optional" htmlFor="settingCloudBucket" hint="留空时复用启动环境或 SDK 默认 Bucket。" error={settingsForm.formState.errors.cloudBucket?.message}>
            <input id="settingCloudBucket" placeholder="agentengine-<account>-cn-beijing-6" {...settingsForm.register("cloudBucket")} />
          </FormField>
        </div>
        <p className="helper">云端部署：{settings?.cloudSignedAccountConfigured ? "已就绪" : "尚未配置"}</p>
      </section>

      <section id="settings-about" className="settings-group" tabIndex={-1}>
        <h3>关于</h3>
        <dl className="trace-detail-grid">
          {about.map(([k, v]) => <div key={k}><dt>{k}</dt><dd>{v}</dd></div>)}
        </dl>
      </section>
      </div>
      </div>

      {configModel && (
        <ModelCredentialDrawer
          model={configModel}
          onClose={() => setConfigModel(null)}
          onChanged={loadCredentials}
        />
      )}
    </Drawer>
    </FormProvider>
  );
}

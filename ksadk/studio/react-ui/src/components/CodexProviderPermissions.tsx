import type { AgentProviderCatalogItem } from "../agentProviders";

// Matches the fixed local Provider reference used by CodexProviderBuildManager.
export const STUDIO_CODEX_PROVIDER_REF = "plugin://io.ksadk.codex-provider@1.0.0";

export function CodexProviderPermissions({ provider, approved, onChange }: {
  provider?: AgentProviderCatalogItem;
  approved: boolean;
  onChange: (approved: boolean) => void;
}) {
  return (
    <div className="template-specific">
      <strong>Codex 本地执行权限</strong>
      {!provider?.selectable ? (
        <p>{provider?.reason?.message || "请在插件中心安装并启用官方 Codex Provider，再构建本地 Agent。"}</p>
      ) : null}
      {provider?.permissions.length ? (
        <label className="post-create-option">
          <input type="checkbox" checked={approved} onChange={event => onChange(event.target.checked)} />
          <span>
            <strong>确认 Codex Provider 请求的权限</strong>
            <small>{provider.permissions.join("、")}；确认后写入此 Agent。安装插件时的同意不代替 Agent 授权。</small>
          </span>
        </label>
      ) : null}
    </div>
  );
}

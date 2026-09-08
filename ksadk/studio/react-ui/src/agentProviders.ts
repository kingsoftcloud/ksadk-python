export interface AgentProviderCatalogItem {
  providerRef: string;
  pluginId: string;
  resolvedVersion: string;
  displayName: string;
  state: "enabled" | "disabled";
  compatible: boolean;
  selectable: boolean;
  reason?: { code: string; message: string } | null;
  permissions: string[];
  isolation: string;
  configSchemaDeclared: boolean;
  secretFields: string[];
}

const SECRET_KEY = /(?:secret|password|token|api[_-]?key)/i;
const SECRET_REF_PREFIXES = ["secret://", "env://", "credential://", "vault://"];

export function parseProviderConfig(
  value: string,
  declaredSecretFields: string[] = [],
): Record<string, unknown> {
  const trimmed = value.trim();
  if (!trimmed) return {};
  let parsed: unknown;
  try {
    parsed = JSON.parse(trimmed);
  } catch {
    throw new Error("Provider 配置必须是合法的 JSON 对象");
  }
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
    throw new Error("Provider 配置必须是 JSON 对象");
  }
  const declared = new Set(
    declaredSecretFields
      .map(field => field.replace(/^providerConfig\./, "").replace(/^\.+|\.+$/g, ""))
      .filter(Boolean),
  );
  assertSecretReferences(parsed, "providerConfig", [], declared);
  return parsed as Record<string, unknown>;
}

function assertSecretReferences(
  value: unknown,
  path: string,
  segments: string[],
  declared: Set<string>,
): void {
  if (Array.isArray(value)) {
    value.forEach((item, index) => assertSecretReferences(item, `${path}[${index}]`, [...segments, String(index)], declared));
    return;
  }
  if (!value || typeof value !== "object") return;
  Object.entries(value as Record<string, unknown>).forEach(([key, child]) => {
    const childPath = `${path}.${key}`;
    const childSegments = [...segments, key];
    const dotted = childSegments.join(".");
    const declaredSecret = declared.has(dotted) || [...declared].some(field => field.split(".").at(-1) === key);
    if (
      (SECRET_KEY.test(key) || declaredSecret)
      && child !== null
      && (typeof child !== "string" || !SECRET_REF_PREFIXES.some(prefix => child.startsWith(prefix)))
    ) {
      throw new Error(`${childPath} 只能填写 Secret 引用，不能填写明文`);
    }
    assertSecretReferences(child, childPath, childSegments, declared);
  });
}

export function providerOptionDescription(item: AgentProviderCatalogItem): string {
  const state = item.selectable ? "已启用" : item.reason?.message || "不可用";
  return `${item.pluginId}@${item.resolvedVersion} · ${state}`;
}

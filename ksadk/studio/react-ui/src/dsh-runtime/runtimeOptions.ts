import type { AgentProviderCatalogItem } from "../agentProviders";
import type { StudioAgentProviderContribution } from "./studioContributions";

export interface StudioRuntimeOption {
  label: string;
  providerRef?: string;
  value: "adk" | "codex" | "langgraph" | "plugin";
}

const BUILTIN_RUNTIME_OPTIONS: readonly StudioRuntimeOption[] = [
  { value: "codex", label: "Codex · ManagedRuntime" },
  { value: "adk", label: "Google ADK · Python source" },
  { value: "langgraph", label: "LangGraph · Python graph" },
];

export function createStudioRuntimeOptions(
  dshProviders: readonly StudioAgentProviderContribution[],
  catalogProviders: readonly AgentProviderCatalogItem[] = [],
): StudioRuntimeOption[] {
  const readyDshProviders = dshProviders.filter(provider => provider.state === "ready" && provider.compatible);
  const providerRefs = new Set([
    ...readyDshProviders.map(provider => provider.providerRef),
    ...catalogProviders.filter(provider => provider.selectable).map(provider => provider.providerRef),
  ]);
  if (providerRefs.size === 0) return [...BUILTIN_RUNTIME_OPTIONS];
  return [
    ...BUILTIN_RUNTIME_OPTIONS,
    {
      value: "plugin",
      label: providerRefs.size === 1
        ? `${readyDshProviders[0]?.displayName || catalogProviders.find(item => item.selectable)?.displayName || "DSH AgentProvider"} · Plugin`
        : `DSH AgentProvider · ${providerRefs.size} 个可用`,
    },
  ];
}

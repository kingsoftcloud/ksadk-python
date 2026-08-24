export function deploymentCreateRoute(buildId: string, agentId?: string): string {
  const params = new URLSearchParams({ buildId });
  if (agentId) params.set("agentId", agentId);
  return `#/deployments/new?${params.toString()}`;
}

export function deploymentDetailRoute(deploymentId: string): string {
  return `#/deployments/${encodeURIComponent(deploymentId)}`;
}

export function navigateToStudioHash(hash: string): void {
  if (window.location.hash === hash) {
    window.dispatchEvent(new HashChangeEvent("hashchange"));
    return;
  }
  window.location.hash = hash;
}

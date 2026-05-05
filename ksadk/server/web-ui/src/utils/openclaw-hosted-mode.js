function normalizeFramework(agentFramework) {
  return String(agentFramework || '').trim().toLowerCase();
}

export function shouldUseOpenClawNativeLauncher(agentFramework) {
  return normalizeFramework(agentFramework) === 'openclaw';
}

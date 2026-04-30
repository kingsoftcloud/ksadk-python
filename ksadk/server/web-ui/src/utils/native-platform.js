const NATIVE_MANAGEMENT_FRAMEWORKS = new Map([
  ['openclaw', 'OpenClaw'],
  ['hermes', 'Hermes'],
]);

function normalizeAccessMode(accessMode) {
  return String(accessMode || '').trim().toLowerCase();
}

function normalizeFramework(agentFramework) {
  return String(agentFramework || '').trim().toLowerCase();
}

function rootUrlFromOrigin(origin) {
  try {
    return new URL('/', origin).toString();
  } catch {
    return '/';
  }
}

export function resolveNativeManagementLink({ agentFramework, accessMode, origin }) {
  const mode = normalizeAccessMode(accessMode);
  if (mode !== 'owner' && mode !== 'private') {
    return null;
  }

  const productName = NATIVE_MANAGEMENT_FRAMEWORKS.get(normalizeFramework(agentFramework));
  if (!productName) {
    return null;
  }

  return {
    href: rootUrlFromOrigin(origin),
    label: '管理平台',
    title: `打开 ${productName} 原生管理平台`,
  };
}

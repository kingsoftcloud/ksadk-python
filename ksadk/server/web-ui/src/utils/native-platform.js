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

export function resolveNativeManagementLinkFromCapability({ capability, agentFramework, accessMode, origin }) {
  const mode = normalizeAccessMode(accessMode);
  if (mode !== 'owner' && mode !== 'private') {
    return null;
  }
  if (!capability?.Enabled) {
    return null;
  }

  const productName = NATIVE_MANAGEMENT_FRAMEWORKS.get(normalizeFramework(agentFramework)) || '运行时';
  const href = typeof capability.Href === 'string' && capability.Href
    ? new URL(capability.Href, rootUrlFromOrigin(origin)).toString()
    : rootUrlFromOrigin(origin);
  const label = typeof capability.Label === 'string' && capability.Label ? capability.Label : '管理平台';

  return {
    href,
    label,
    title: `打开 ${productName} 原生管理平台`,
  };
}

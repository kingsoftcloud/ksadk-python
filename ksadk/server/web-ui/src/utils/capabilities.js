const DEFAULT_API_FORMATS = ['responses', 'chat_completions'];
const NATIVE_DASHBOARD_FRAMEWORKS = new Map([
  ['openclaw', 'OpenClaw'],
  ['hermes', 'Hermes'],
]);

function normalizeFramework(value) {
  return String(value || '').trim().toLowerCase();
}

function firstFramework(...values) {
  for (const value of values) {
    const framework = normalizeFramework(value);
    if (framework) {
      return framework;
    }
  }
  return '';
}

function asObject(value) {
  return value && typeof value === 'object' && !Array.isArray(value) ? value : {};
}

function normalizeApiFormats(value) {
  if (!Array.isArray(value)) {
    return DEFAULT_API_FORMATS;
  }
  const formats = value.filter((item) => item === 'responses' || item === 'chat_completions');
  return formats.length > 0 ? formats : DEFAULT_API_FORMATS;
}

function normalizeEnabled(value, fallback) {
  return typeof value === 'boolean' ? value : fallback;
}

export function normalizeCapabilities(bootstrap) {
  const data = bootstrap?.Data || bootstrap || {};
  const rawCapabilities = asObject(data.Capabilities);
  const hostedRuntime = asObject(data.HostedRuntime);
  const framework = firstFramework(
    data.Agent?.Framework,
    hostedRuntime.Framework,
    hostedRuntime.Type,
  );
  const productName = NATIVE_DASHBOARD_FRAMEWORKS.get(framework) || '';
  const apiFormats = normalizeApiFormats(data.ApiFormats || rawCapabilities.HostedChat?.ApiFormats);
  const defaultHostedChat = framework !== 'openclaw';
  const defaultNativeDashboard = Boolean(productName);
  const defaultNativeTerminal = Boolean(productName);

  const hostedChat = asObject(rawCapabilities.HostedChat);
  const nativeDashboard = asObject(rawCapabilities.NativeDashboard);
  const nativeTerminal = asObject(rawCapabilities.NativeTerminal);
  const runLifecycle = asObject(rawCapabilities.RunLifecycle);

  const hostedChatEnabled = normalizeEnabled(hostedChat.Enabled, defaultHostedChat);
  const nativeDashboardEnabled = normalizeEnabled(
    nativeDashboard.Enabled,
    defaultNativeDashboard,
  );
  const nativeTerminalEnabled = normalizeEnabled(nativeTerminal.Enabled, defaultNativeTerminal);
  const runLifecycleEnabled = normalizeEnabled(runLifecycle.Enabled, hostedChatEnabled);

  return {
    ...rawCapabilities,
    HostedChat: {
      Enabled: hostedChatEnabled,
      ApiFormats: normalizeApiFormats(hostedChat.ApiFormats || apiFormats),
    },
    NativeDashboard: {
      Enabled: nativeDashboardEnabled,
      Href:
        typeof nativeDashboard.Href === 'string'
          ? nativeDashboard.Href
          : nativeDashboardEnabled
            ? '/'
            : null,
      Label:
        typeof nativeDashboard.Label === 'string'
          ? nativeDashboard.Label
          : nativeDashboardEnabled
            ? '管理平台'
            : null,
    },
    NativeTerminal: {
      Enabled: nativeTerminalEnabled,
      Mode:
        typeof nativeTerminal.Mode === 'string'
          ? nativeTerminal.Mode
          : nativeTerminalEnabled
            ? 'tui'
            : null,
      Protocol:
        typeof nativeTerminal.Protocol === 'string'
          ? nativeTerminal.Protocol
          : 'ks-terminal.v1',
      Path:
        typeof nativeTerminal.Path === 'string'
          ? nativeTerminal.Path
          : '/_ksadk/terminal/ws',
    },
    RunLifecycle: {
      Enabled: runLifecycleEnabled,
      Resume: normalizeEnabled(runLifecycle.Resume, runLifecycleEnabled),
      Abort: normalizeEnabled(runLifecycle.Abort, runLifecycleEnabled),
    },
  };
}

export function isHostedChatEnabled(capabilities) {
  return normalizeCapabilities({ Data: { Capabilities: capabilities } }).HostedChat.Enabled;
}

export function isNativeDashboardEnabled(capabilities) {
  return normalizeCapabilities({ Data: { Capabilities: capabilities } }).NativeDashboard.Enabled;
}

export function isNativeTerminalEnabled(capabilities) {
  return normalizeCapabilities({ Data: { Capabilities: capabilities } }).NativeTerminal.Enabled;
}

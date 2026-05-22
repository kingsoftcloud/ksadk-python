import test from 'node:test';
import assert from 'node:assert/strict';

async function loadCapabilityUtils() {
  return import('../src/utils/capabilities.js').catch(() => null);
}

test('capabilities default generic frameworks to hosted chat', async () => {
  const capabilities = await loadCapabilityUtils();

  assert.ok(capabilities, 'expected capability helpers to exist');
  for (const framework of ['', 'default', 'langgraph', 'langchain', 'adk', 'deepagents', 'unknown']) {
    const normalized = capabilities.normalizeCapabilities({
      Data: {
        Agent: { Framework: framework },
        ApiFormats: ['responses', 'chat_completions'],
        Capabilities: {},
      },
    });
    assert.equal(capabilities.isHostedChatEnabled(normalized), true, framework);
    assert.equal(capabilities.isNativeDashboardEnabled(normalized), false, framework);
    assert.equal(capabilities.isNativeTerminalEnabled(normalized), false, framework);
  }
});

test('capabilities fallback keeps OpenClaw native and Hermes hybrid behavior', async () => {
  const capabilities = await loadCapabilityUtils();

  assert.ok(capabilities, 'expected capability helpers to exist');
  const openclaw = capabilities.normalizeCapabilities({
    Data: { Agent: { Framework: 'OpenClaw' }, Capabilities: {} },
  });
  assert.equal(capabilities.isHostedChatEnabled(openclaw), false);
  assert.equal(capabilities.isNativeDashboardEnabled(openclaw), true);
  assert.equal(capabilities.isNativeTerminalEnabled(openclaw), true);

  const hermes = capabilities.normalizeCapabilities({
    Data: { Agent: { Framework: 'hermes' }, Capabilities: {} },
  });
  assert.equal(capabilities.isHostedChatEnabled(hermes), true);
  assert.equal(capabilities.isNativeDashboardEnabled(hermes), true);
  assert.equal(capabilities.isNativeTerminalEnabled(hermes), true);
});

test('capabilities fallback can infer native terminal from hosted runtime metadata', async () => {
  const capabilities = await loadCapabilityUtils();

  assert.ok(capabilities, 'expected capability helpers to exist');
  for (const framework of ['hermes', 'openclaw']) {
    const normalized = capabilities.normalizeCapabilities({
      Data: {
        Agent: {},
        HostedRuntime: { Framework: framework },
        Capabilities: {},
      },
    });

    assert.equal(capabilities.isNativeTerminalEnabled(normalized), true, framework);
  }
});

test('explicit capability objects override framework fallback', async () => {
  const capabilities = await loadCapabilityUtils();

  assert.ok(capabilities, 'expected capability helpers to exist');
  const normalized = capabilities.normalizeCapabilities({
    Data: {
      Agent: { Framework: 'langgraph' },
      Capabilities: {
        HostedChat: { Enabled: false },
        NativeDashboard: {
          Enabled: true,
          Href: '/dashboard',
          Label: '原生入口',
        },
        NativeTerminal: {
          Enabled: true,
          Mode: 'tui',
          Protocol: 'ks-terminal.v1',
          Path: '/_ksadk/terminal/ws',
        },
      },
    },
  });

  assert.equal(capabilities.isHostedChatEnabled(normalized), false);
  assert.equal(capabilities.isNativeDashboardEnabled(normalized), true);
  assert.equal(capabilities.isNativeTerminalEnabled(normalized), true);
  assert.deepEqual(normalized.NativeDashboard, {
    Enabled: true,
    Href: '/dashboard',
    Label: '原生入口',
  });
});

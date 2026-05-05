import test from 'node:test';
import assert from 'node:assert/strict';

async function loadNativePlatformUtils() {
  return import('../src/utils/native-platform.js').catch(() => null);
}

test('native management link is only available for owner/private framework UIs', async () => {
  const nativePlatform = await loadNativePlatformUtils();

  assert.ok(nativePlatform, 'expected native platform helpers to exist');
  assert.deepEqual(
    nativePlatform.resolveNativeManagementLink({
      agentFramework: 'openclaw',
      accessMode: 'Owner',
      origin: 'https://agent.example.com',
    }),
    {
      href: 'https://agent.example.com/',
      label: '管理平台',
      title: '打开 OpenClaw 原生管理平台',
    },
  );
  assert.deepEqual(
    nativePlatform.resolveNativeManagementLink({
      agentFramework: 'hermes',
      accessMode: 'Private',
      origin: 'https://agent.example.com/chat',
    }),
    {
      href: 'https://agent.example.com/',
      label: '管理平台',
      title: '打开 Hermes 原生管理平台',
    },
  );
  assert.equal(
    nativePlatform.resolveNativeManagementLink({
      agentFramework: 'openclaw',
      accessMode: 'Share',
      origin: 'https://agent.example.com',
    }),
    null,
  );
  assert.equal(
    nativePlatform.resolveNativeManagementLink({
      agentFramework: 'langgraph',
      accessMode: 'Owner',
      origin: 'https://agent.example.com',
    }),
    null,
  );
});

test('native management link can be resolved from capability objects', async () => {
  const nativePlatform = await loadNativePlatformUtils();

  assert.ok(nativePlatform, 'expected native platform helpers to exist');
  assert.deepEqual(
    nativePlatform.resolveNativeManagementLinkFromCapability({
      capability: {
        Enabled: true,
        Href: '/',
        Label: '管理平台',
      },
      agentFramework: 'hermes',
      accessMode: 'Owner',
      origin: 'https://agent.example.com/chat/',
    }),
    {
      href: 'https://agent.example.com/',
      label: '管理平台',
      title: '打开 Hermes 原生管理平台',
    },
  );
  assert.equal(
    nativePlatform.resolveNativeManagementLinkFromCapability({
      capability: {
        Enabled: true,
        Href: '/',
        Label: '管理平台',
      },
      agentFramework: 'openclaw',
      accessMode: 'Share',
      origin: 'https://agent.example.com/chat/',
    }),
    null,
  );
});

import { readFileSync } from 'node:fs';
import { runInNewContext } from 'node:vm';
import { test } from 'node:test';
import assert from 'node:assert/strict';

const source = readFileSync(new URL('../../../plugins/providers/bundles/ksadk-dsh-studio/client.js', import.meta.url), 'utf8');

function sectionsFor(modules) {
  let plugin, root;
  const window = {
    __ModuleLoader__: { load: value => { plugin = value; } },
    __DSH_BOOT__: { entries: Object.keys(modules).map(id => ({ id })) },
    addEventListener() {}, removeEventListener() {},
  };
  runInNewContext(source, { window });
  const react = {
    Component: class {}, createElement() {}, Fragment: 'fragment',
    useEffect: effect => effect(), useRef: () => ({ current: null }),
    useState: () => [null, () => {}],
  };
  plugin.factory(id => {
    if (id === 'react') return react;
    if (id === 'react-dom') return { createPortal() {} };
    if (!(id in modules)) throw new Error('Module not loaded');
    return modules[id];
  }).apply({ slots: {
    register: (_, component) => { root = component; },
    subscribe: () => () => {},
    entriesOfSlot: () => [{ registrant: 'im-settings', options: { id: 'xmanrui-dsh-im', label: 'IM机器人' } }],
  } });
  root({ renderSlot() {} });
  return window.__STUDIO_DSH__.sections();
}

test('maps the official client export name to its npm package for detail navigation', () => {
  const sections = sectionsFor({ '@xmanrui/dsh-im': { name: 'im-settings' }, '@example/ssh': { name: 'ssh-settings' } });
  assert.equal(sections[0].pluginId, '@xmanrui/dsh-im');
  assert.equal(sections[0].id, 'xmanrui-dsh-im');
});

test('keeps ambiguous or unknown registrants available globally without assigning a package', () => {
  for (const modules of [{}, { '@example/a': { name: 'im-settings' }, '@example/b': { name: 'im-settings' } }]) {
    const sections = sectionsFor(modules);
    assert.equal(sections.length, 1);
    assert.equal(sections[0].pluginId, undefined);
  }
});

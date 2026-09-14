window.__ModuleLoader__.load({
  id: '@kingsoftcloud/dsh-app-studio',
  factory: require => {
    const { Component, createElement, Fragment, useEffect, useRef, useState } = require('react');
    const { createPortal } = require('react-dom');

    class PluginSurfaceBoundary extends Component {
      state = { failed: false };
      static getDerivedStateFromError() { return { failed: true }; }
      render() {
        return this.state.failed
          ? createElement('p', { role: 'alert' }, '此插件页面加载失败，可以返回列表选择其他插件。')
          : this.props.children;
      }
    }

    function sectionLabel(entry) {
      try {
        const label = typeof entry.options.label === 'function' ? entry.options.label() : entry.options.label;
        return typeof label === 'string' && label ? label : entry.options.id;
      } catch { return entry.options.id; }
    }

    function apply(ctx) {
      const mounts = new Map();
      const listeners = new Set();
      const notify = () => listeners.forEach(listener => listener());
      const workspaceListeners = new Set();
      const notifyWorkspace = () => {
        workspaceListeners.forEach(listener => listener());
        notify();
        window.dispatchEvent?.(new Event('studio:workspace-changed'));
      };
      function packageForSection(entry) {
        const registrant = entry.registrant || entry.options.registrant;
        if (!registrant) return undefined;
        // Cordis uses the client export's name, which need not equal its npm ID.
        // Resolve only the official boot graph; ambiguous names stay unassigned.
        const matches = (window.__DSH_BOOT__?.entries || []).filter(row => {
          try { return require(row.id).name === registrant; }
          catch { return false; } // A deferred module may not have arrived yet.
        });
        return matches.length === 1 ? matches[0].id : undefined;
      }
      const bridge = {
        sections: () => ctx.slots.entriesOfSlot('settings.section')
          .filter(entry => typeof entry.options.id === 'string' && !['general', 'models', 'plugins', 'agent-presets'].includes(entry.options.id))
          .sort((a, b) => (a.options.order || 0) - (b.options.order || 0))
          .map(entry => ({
            id: entry.options.id,
            label: sectionLabel(entry),
            // Display/navigation attribution only, never an authorization key.
            pluginId: packageForSection(entry),
          })),
        subscribe: listener => { listeners.add(listener); return () => listeners.delete(listener); },
        attach: (id, container, close) => {
          const key = Symbol(id);
          mounts.set(key, { id, container, props: { close }, slot: 'settings.section' }); notify();
          return () => { mounts.delete(key); notify(); };
        },
        workspacePages: () => ctx.slots.entriesOfSlot('studio.workspace.page')
          .filter(entry => typeof entry.options.id === 'string')
          .sort((a, b) => (a.options.order || 0) - (b.options.order || 0))
          .map(entry => ({ id: entry.options.id, label: sectionLabel(entry),
            pluginId: packageForSection(entry), order: entry.options.order || 0 })),
        subscribeWorkspace: listener => {
          workspaceListeners.add(listener); return () => workspaceListeners.delete(listener);
        },
        attachWorkspace: (id, container, props = {}) => {
          const key = Symbol(id);
          mounts.set(key, { id, container, props, slot: 'studio.workspace.page' }); notify();
          return () => { mounts.delete(key); notify(); };
        },
      };
      function StudioRoot({ renderSlot }) {
        const container = useRef(null);
        const [, update] = useState(0);
        const [error, setError] = useState('');
        useEffect(() => {
          let disposed = false, unmount;
          const redraw = () => update(value => value + 1);
          const detach = bridge.subscribe(redraw);
          const unsubscribe = ctx.slots.subscribe('settings.section', notify);
          const unsubscribeWorkspace = ctx.slots.subscribe('studio.workspace.page', notifyWorkspace);
          window.__STUDIO_DSH__ = bridge;
          window.dispatchEvent?.(new Event('studio:bridge-ready'));
          const mount = () => {
            if (!window.__STUDIO_APP__ || !container.current) return;
            window.removeEventListener('studio:app-ready', mount);
            Promise.resolve(window.__STUDIO_APP__.mount(container.current)).then(dispose => {
              if (disposed) dispose(); else unmount = dispose;
            }).catch(cause => setError(String(cause)));
          };
          window.addEventListener('studio:app-ready', mount); mount();
          return () => {
            disposed = true; detach(); unsubscribe(); unsubscribeWorkspace(); unmount?.();
            window.removeEventListener('studio:app-ready', mount);
            if (window.__STUDIO_DSH__ === bridge) delete window.__STUDIO_DSH__;
            window.dispatchEvent?.(new Event('studio:workspace-changed'));
          };
        }, []);
        return createElement(Fragment, null,
          createElement('div', { ref: container, className: 'studio-native-root', style: { height: '100vh' } }, error || null),
          ...[...mounts.values()].map(({ id, container, props, slot }) => createPortal(
            createElement(PluginSurfaceBoundary, { key: id }, renderSlot(slot, props, { only: id })), container, id)),
        );
      }
      ctx.slots.register({ name: 'root', priority: -100, children: {
        'settings.section': { kind: 'list', scope: 'root' },
        'studio.workspace.page': { kind: 'list', scope: 'root' },
      } }, StudioRoot);
    }
    return { name: 'studio-app', inject: ['slots'], apply };
  },
});

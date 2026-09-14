window.__ModuleLoader__.load({
  id: '@kingsoftcloud/dsh-teams-client',
  factory: require => {
    const { createElement, useEffect, useRef, useState } = require('react');
    function TeamsWorkspace(props) {
      const container = useRef(null);
      const [error, setError] = useState('');
      useEffect(() => {
        let disposed = false, detach;
        const mount = window.__STUDIO_APP__?.mountWorkspace;
        if (!mount) { setError('Studio 团队组件不可用'); return; }
        Promise.resolve(mount('teams', container.current, props)).then(value => {
          if (disposed) value?.(); else detach = value;
        }).catch(() => setError('团队页面加载失败'));
        return () => { disposed = true; detach?.(); };
      }, []);
      return createElement('div', { ref: container, style: { height: '100%', minHeight: 0 } },
        error ? createElement('p', { role: 'alert' }, error) : null);
    }
    return {
      name: 'dsh-teams-client', inject: ['slots'],
      apply(ctx) {
        ctx.slots.inject('studio.workspace.page', () => ctx.slots.register({
          name: 'studio.workspace.page', id: 'teams', label: '团队', order: 40,
        }, TeamsWorkspace));
      },
    };
  },
});

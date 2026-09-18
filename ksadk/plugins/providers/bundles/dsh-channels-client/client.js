window.__ModuleLoader__.load({
  id: '@kingsoftcloud/dsh-channels-client',
  factory: require => {
    const { createElement, useEffect, useRef, useState } = require('react');
    function ChannelsWorkspace(props) {
      const container = useRef(null);
      const [error, setError] = useState('');
      useEffect(() => {
        let disposed = false, detach;
        const mount = window.__STUDIO_APP__?.mountWorkspace;
        if (!mount) { setError('Studio 消息渠道组件不可用'); return; }
        Promise.resolve(mount('channels', container.current, props)).then(value => {
          if (disposed) value?.(); else detach = value;
        }).catch(() => setError('消息渠道页面加载失败'));
        return () => { disposed = true; detach?.(); };
      }, []);
      return createElement('div', { ref: container, style: { height: '100%', minHeight: 0 } },
        error ? createElement('p', { role: 'alert' }, error) : null);
    }
    return {
      name: 'dsh-channels-client', inject: ['slots'],
      apply(ctx) {
        ctx.slots.inject('studio.workspace.page', () => ctx.slots.register({
          name: 'studio.workspace.page', id: 'channels', label: '消息渠道', order: 45,
        }, ChannelsWorkspace));
      },
    };
  },
});

import { useEffect, useRef, useState } from 'react';
import { ArrowLeft, LoaderCircle } from 'lucide-react';
import { apiFetch } from '../api';

/** Mount official slot contributions inside Studio, preserving their Core context. */
export function DshPluginWorkspace({ onBack, pluginId }: { onBack: () => void; pluginId?: string }) {
  const bridge = window.__STUDIO_DSH__;
  const [sections, setSections] = useState(() => bridge?.sections() || []);
  const [selected, setSelected] = useState('');
  const [error, setError] = useState('');
  const [attempt, setAttempt] = useState(0);
  const container = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!bridge) {
      const controller = new AbortController();
      void apiFetch('/api/v1/plugin-ecosystems/dsh/core/session', { method: 'POST', signal: controller.signal })
        .then(async response => {
          if (!response.ok) throw new Error((await response.json())?.error?.message || '插件服务启动失败');
          const query = new URLSearchParams({ pluginSettings: pluginId || '' });
          window.location.assign(`/studio-core/?${query}#/plugins`);
        }).catch(cause => { if (!controller.signal.aborted) setError(String(cause.message || cause)); });
      return () => controller.abort();
    }
    const update = () => setSections(bridge.sections());
    update();
    return bridge.subscribe(update);
  }, [bridge, attempt, pluginId]);
  const visibleSections = pluginId ? sections.filter(section => section.pluginId === pluginId) : sections;
  const active = visibleSections.some(section => section.id === selected) ? selected
    : pluginId && visibleSections.length === 1 ? visibleSections[0].id : undefined;
  useEffect(() => {
    if (bridge && active && container.current) return bridge.attach(active, container.current, onBack);
  }, [bridge, active, onBack]);
  return <section className="dsh-plugin-workspace" aria-label="插件设置">
    <header><button className="button tertiary" onClick={active && !pluginId ? () => setSelected('') : onBack}><ArrowLeft size={16}/>{active && !pluginId ? '所有插件设置' : '返回插件'}</button><h2>插件设置</h2></header>
    {error && <div><p className="form-error" role="alert">{error}</p><button className="button secondary" onClick={() => { setError(''); setAttempt(value => value + 1); }}>重试</button></div>}
    {!bridge && !error && <p role="status"><LoaderCircle className="animate-spin" size={16}/>正在准备插件…</p>}
    {bridge && <div className={`studio-plugin-settings${active ? '' : ' choosing'}`}>
      {!active && !!visibleSections.length && <p>{pluginId ? '选择设置页面' : '选择要配置的插件'}</p>}
      <nav aria-label="插件设置页面">{visibleSections.map(section => <button key={section.id} data-plugin-id={section.pluginId} aria-current={active === section.id ? 'page' : undefined} className={active === section.id ? 'active' : ''} onClick={() => setSelected(section.id)}>{section.label}</button>)}</nav>
      {active && <div ref={container} className="studio-plugin-surface"/>}
      {!visibleSections.length && <p>当前插件没有提供设置页面。</p>}
    </div>}
  </section>;
}

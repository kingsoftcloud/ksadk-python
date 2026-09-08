import { act, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, expect, it, vi } from 'vitest';
import { apiFetch } from '../api';
import { DshPluginWorkspace } from './DshPluginWorkspace';

vi.mock('../api', () => ({ apiFetch: vi.fn() }));
afterEach(() => { delete window.__STUDIO_DSH__; vi.clearAllMocks(); });

it('opens the explicitly requested plugin directly without selecting another plugin', () => {
  const attach = vi.fn(() => vi.fn());
  window.__STUDIO_DSH__ = {
    sections: () => [
      { id: 'ssh-settings', label: 'SSH', pluginId: '@example/ssh' },
      { id: 'im-settings', label: 'IM机器人', pluginId: '@example/im' },
    ], subscribe: () => () => {}, attach,
  };
  render(<DshPluginWorkspace pluginId="@example/im" onBack={() => {}}/>);
  expect(attach).toHaveBeenCalledWith('im-settings', expect.any(HTMLElement), expect.any(Function));
  expect(screen.queryByText('选择要配置的插件')).not.toBeInTheDocument();
  expect(screen.queryByRole('button', { name: 'SSH' })).not.toBeInTheDocument();
});

it('asks only which page when the chosen plugin contributes several pages', async () => {
  const attach = vi.fn(() => vi.fn());
  window.__STUDIO_DSH__ = {
    sections: () => [
      { id: 'im', label: 'IM机器人', pluginId: '@example/im' },
      { id: 'ssh-hosts', label: '主机', pluginId: '@example/ssh' },
      { id: 'ssh-keys', label: '密钥', pluginId: '@example/ssh' },
    ], subscribe: () => () => {}, attach,
  };
  render(<DshPluginWorkspace pluginId="@example/ssh" onBack={() => {}}/>);
  expect(attach).not.toHaveBeenCalled();
  expect(screen.getByText('选择设置页面')).toBeInTheDocument();
  expect(screen.queryByRole('button', { name: 'IM机器人' })).not.toBeInTheDocument();
  await userEvent.click(screen.getByRole('button', { name: '密钥' }));
  expect(attach).toHaveBeenCalledWith('ssh-keys', expect.any(HTMLElement), expect.any(Function));
});

it('never substitutes another plugin when the requested plugin has no active page', () => {
  const attach = vi.fn(() => vi.fn());
  window.__STUDIO_DSH__ = { sections: () => [{ id: 'im', label: 'IM机器人', pluginId: '@example/im' }], subscribe: () => () => {}, attach };
  render(<DshPluginWorkspace pluginId="@example/missing" onBack={() => {}}/>);
  expect(screen.getByText('当前插件没有提供设置页面。')).toBeInTheDocument();
  expect(attach).not.toHaveBeenCalled();
});

it('does not select even a single UI plugin before the user chooses', async () => {
  const attach = vi.fn(() => vi.fn());
  window.__STUDIO_DSH__ = { sections: () => [{ id: 'im', label: 'IM机器人' }], subscribe: () => () => {}, attach };
  const { container } = render(<DshPluginWorkspace onBack={() => {}}/>);
  expect(screen.getByText('选择要配置的插件')).toBeInTheDocument();
  expect(attach).not.toHaveBeenCalled();
  expect(container.querySelector('iframe')).toBeNull();
  await userEvent.click(screen.getByRole('button', { name: 'IM机器人' }));
  expect(attach).toHaveBeenCalledWith('im', expect.any(HTMLElement), expect.any(Function));
});

it('switches multiple plugin surfaces, detaches the old surface and handles removal', async () => {
  let sections = [{ id: 'im', label: 'IM机器人' }, { id: 'ssh', label: 'SSH主机' }];
  let notify = () => {};
  const detached: string[] = [];
  const attach = vi.fn((id: string) => () => { detached.push(id); });
  window.__STUDIO_DSH__ = { sections: () => sections, subscribe: cb => { notify = cb; return () => {}; }, attach };
  render(<DshPluginWorkspace onBack={() => {}}/>);
  expect(attach).not.toHaveBeenCalled();
  await userEvent.click(screen.getByRole('button', { name: 'IM机器人' }));
  await userEvent.click(screen.getByRole('button', { name: 'SSH主机' }));
  expect(detached).toEqual(['im']);
  expect(screen.getByRole('button', { name: 'SSH主机' })).toHaveAttribute('aria-current', 'page');
  act(() => { sections = [sections[0]]; notify(); });
  expect(detached).toEqual(['im', 'ssh']);
  expect(screen.getByText('选择要配置的插件')).toBeInTheDocument();
  expect(attach).toHaveBeenCalledTimes(2);
});

it('returns from a selected plugin to the chooser without leaving Studio', async () => {
  const detach = vi.fn(), onBack = vi.fn();
  window.__STUDIO_DSH__ = { sections: () => [{ id: 'im', label: 'IM机器人' }], subscribe: () => () => {}, attach: () => detach };
  render(<DshPluginWorkspace onBack={onBack}/>);
  await userEvent.click(screen.getByRole('button', { name: 'IM机器人' }));
  await userEvent.click(screen.getByRole('button', { name: '所有插件设置' }));
  expect(detach).toHaveBeenCalledOnce();
  expect(onBack).not.toHaveBeenCalled();
  await userEvent.click(screen.getByRole('button', { name: '返回插件' }));
  expect(onBack).toHaveBeenCalledOnce();
});

it('keeps bootstrap errors visible and supports retry', async () => {
  vi.mocked(apiFetch).mockResolvedValue({ ok: false, json: async () => ({ error: { message: 'Core不可用' } }) } as Response);
  render(<DshPluginWorkspace onBack={() => {}}/>);
  expect(await screen.findByRole('alert')).toHaveTextContent('Core不可用');
  await userEvent.click(screen.getByRole('button', { name: '重试' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('Core不可用');
  expect(apiFetch).toHaveBeenCalledTimes(2);
});

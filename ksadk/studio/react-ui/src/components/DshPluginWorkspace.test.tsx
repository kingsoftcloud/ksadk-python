import { act, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, expect, it, vi } from 'vitest';
import { apiFetch } from '../api';
import { DshPluginWorkspace } from './DshPluginWorkspace';

vi.mock('../api', () => ({ apiFetch: vi.fn() }));
afterEach(() => { delete window.__STUDIO_DSH__; vi.clearAllMocks(); });

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

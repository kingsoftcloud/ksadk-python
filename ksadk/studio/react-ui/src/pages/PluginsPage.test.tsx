import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { apiFetch } from "../api";
import { showToast } from "../components/Toast";
import { normalizeInstalledPlugin, PluginsPage } from "./PluginsPage";

vi.mock("../api", () => ({ apiFetch: vi.fn() }));
vi.mock("../components/Toast", () => ({ showToast: vi.fn() }));
const mockedFetch = vi.mocked(apiFetch);
const mockedToast = vi.mocked(showToast);
const response = (payload: unknown) => ({ ok: true, status: 200, json: async () => payload }) as Response;

describe("PluginsPage", () => {
  beforeEach(() => {
    mockedFetch.mockReset();
    mockedToast.mockReset();
  });

  it("rejects the removed third ecosystem", () => {
    expect(() => normalizeInstalledPlugin({ ecosystem: "ksadk", pluginId: "x" })).toThrow("不支持的插件生态");
  });

  it("keeps management enablement separate from provider readiness and binding", () => {
    expect(normalizeInstalledPlugin({
      ecosystem: "dsh", pluginId: "installed", installed: true, enabled: false, state: "disabled",
    })).toMatchObject({ state: "installed", installed: true, enabled: false, ready: false, bound: false, failed: false });
    expect(normalizeInstalledPlugin({
      ecosystem: "dsh", pluginId: "enabled", installed: true, enabled: true, state: "enabled",
    })).toMatchObject({ state: "enabled", enabled: true, ready: false, bound: false, failed: false });
    expect(normalizeInstalledPlugin({
      ecosystem: "dsh", pluginId: "ready", state: "enabled", runtimeState: { state: "ready", providerRef: "plugin://ready" },
    })).toMatchObject({ state: "ready", enabled: true, ready: true, bound: false, providerRef: "plugin://ready" });
    expect(normalizeInstalledPlugin({
      ecosystem: "dsh", pluginId: "bound", state: "enabled", runtime_state: { state: "bound", provider_ref: "plugin://bound" },
    })).toMatchObject({ state: "bound", enabled: true, ready: true, bound: true, providerRef: "plugin://bound" });
    expect(normalizeInstalledPlugin({
      ecosystem: "dsh", pluginId: "failed", enabled: true, runtimeState: { state: "failed", errorCode: "BROKEN" },
    })).toMatchObject({ state: "failed", enabled: true, ready: false, bound: false, failed: true, errorCode: "BROKEN" });
  });

  function catalog(codex: any[] = [], dsh: any[] = []) {
    mockedFetch.mockImplementation(async input => {
      const path = String(input);
      if (path.includes('/dsh/')) return response({ host: { available: true }, items: dsh });
      if (path.includes('/plugins/')) {
        const item = codex.find(item => path.includes(encodeURIComponent(item.pluginId)));
        return response({ item, capabilities: { skills: ['review'], mcpServers: [] } });
      }
      return response({ host: { available: true }, items: codex });
    });
  }

  it('lists plugins and opens an explicitly chosen detail', async () => {
    catalog([], [{ ecosystem: 'dsh', pluginId: '@example/im', displayName: 'IM', enabled: true }]);
    render(<PluginsPage/>);
    await userEvent.click(await screen.findByRole('button', { name: 'IM' }));
    expect(screen.getByRole('article', { name: '插件详情' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '打开插件设置' })).toBeInTheDocument();
    expect(screen.getByText('已启用')).toBeInTheDocument();
  });

  it('does not render a provider as a DSH UI plugin', async () => {
    catalog([], [{ ecosystem: 'dsh', pluginId: '@kingsoftcloud/ksadk-codex-provider', runtimeState: { state: 'ready', providerRef: 'plugin://provider@1' } }]);
    render(<PluginsPage/>);
    await userEvent.click(await screen.findByRole('button', { name: 'Codex AgentProvider' }));
    expect(screen.getByRole('link', { name: '去创建 Agent' })).toHaveAttribute('href', '#/create');
    expect(screen.queryByRole('button', { name: '打开插件设置' })).not.toBeInTheDocument();
  });

  it('searches the catalog and switches to DSH source installation', async () => {
    catalog([{ ecosystem: 'codex', pluginId: 'gmail', displayName: 'Gmail', installed: false }, { ecosystem: 'codex', pluginId: 'figma', displayName: 'Figma', installed: false }]);
    render(<PluginsPage/>);
    await screen.findByText('Figma');
    await userEvent.type(screen.getByRole('textbox', { name: '搜索插件' }), 'gmail');
    expect(screen.getByText('Gmail')).toBeInTheDocument();
    expect(screen.queryByText('Figma')).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole('tab', { name: 'DeepSeek Harness 插件' }));
    expect(screen.getByLabelText('DSH 插件来源')).toHaveValue('');
  });

  it('displays real descriptions, examples and artwork in the detail', async () => {
    catalog([{ ecosystem: 'codex', pluginId: 'github', displayName: 'GitHub', installed: false, interface: {
      logoUrl: 'https://example.com/github.png', shortDescription: 'Review code', longDescription: 'Inspect repositories',
      defaultPrompt: ['Review this change'], developerName: 'OpenAI', category: 'Developer Tools',
    }, description: 'Review code' }]);
    render(<PluginsPage/>);
    await userEvent.click(await screen.findByRole('button', { name: /GitHub/ }));
    expect(screen.getByText('Inspect repositories')).toBeInTheDocument();
    expect(screen.getByText('Review this change')).toBeInTheDocument();
    expect(screen.getByRole('article').querySelector('img')).toHaveAttribute('src', 'https://example.com/github.png');
    expect(await screen.findByRole('link', { name: '去 Agent 列表绑定' })).toBeInTheDocument();
  });

  it('does not repeat the summary when there is no distinct long description', async () => {
    catalog([{ ecosystem: 'codex', pluginId: 'sample', displayName: 'Sample', installed: false, description: 'A useful plugin' }]);
    render(<PluginsPage/>);
    await userEvent.click(await screen.findByRole('button', { name: /Sample/ }));
    expect(screen.getAllByText('A useful plugin')).toHaveLength(1);
  });

  it('installs official Codex plugins on click without an extra trust checkbox', async () => {
    const item = { ecosystem: 'codex', pluginId: 'github', displayName: 'GitHub', marketplaceName: 'openai-curated', installed: false };
    catalog([item]);
    const implementation = mockedFetch.getMockImplementation()!;
    mockedFetch.mockImplementation(async (input, init) => String(input).endsWith(':install')
      ? response({ item: { ...item, installed: true, enabled: true } }) : implementation(input, init));
    render(<PluginsPage/>);
    await userEvent.click(await screen.findByRole('button', { name: /GitHub/ }));
    expect(screen.queryByRole('checkbox')).not.toBeInTheDocument();
    const button = screen.getByRole('button', { name: '安装' });
    expect(button).toBeEnabled();
    await userEvent.click(button);
    await waitFor(() => expect(mockedFetch).toHaveBeenCalledWith(
      '/api/v1/plugin-ecosystems/codex/plugins/github:install',
      expect.objectContaining({ method: 'POST', body: JSON.stringify({ marketplaceName: 'openai-curated', acceptUndeclaredPermissions: true }) }),
    ));
  });

  it('keeps consent for third-party sources and resets consent between plugins', async () => {
    catalog(['one', 'two'].map(id => ({ ecosystem: 'codex', pluginId: id, displayName: id, marketplaceName: 'community', installed: false })));
    render(<PluginsPage/>);
    await userEvent.click(await screen.findByRole('button', { name: /one/ }));
    expect(screen.getByRole('button', { name: '安装' })).toBeDisabled();
    await userEvent.click(screen.getByRole('checkbox'));
    expect(screen.getByRole('button', { name: '安装' })).toBeEnabled();
    await userEvent.click(screen.getByRole('button', { name: '插件' }));
    await userEvent.click(screen.getByRole('button', { name: /two/ }));
    expect(screen.getByRole('button', { name: '安装' })).toBeDisabled();
  });

  it('does not automatically enable a newly installed DSH plugin', async () => {
    catalog();
    const implementation = mockedFetch.getMockImplementation()!;
    mockedFetch.mockImplementation(async (input, init) => String(input).endsWith('/plugins:install')
      ? response({ item: { ecosystem: 'dsh', pluginId: 'demo', installed: true, enabled: false } }) : implementation(input, init));
    render(<PluginsPage/>);
    await userEvent.click(await screen.findByRole('tab', { name: 'DeepSeek Harness 插件' }));
    await userEvent.type(screen.getByLabelText('DSH 插件来源'), '@example/demo@1.0.0');
    expect(screen.getByRole('button', { name: '安装' })).toBeDisabled();
    await userEvent.click(screen.getByRole('checkbox'));
    await userEvent.click(screen.getByRole('button', { name: '安装' }));
    await waitFor(() => expect(mockedToast).toHaveBeenCalledWith('已安装，待启用', 'demo'));
    expect(mockedFetch.mock.calls.some(([input]) => String(input).endsWith(':enable'))).toBe(false);
  });

  it('keeps loading visible before the catalog responds', () => {
    mockedFetch.mockImplementation(async () => await new Promise<Response>(() => {}));
    render(<PluginsPage/>);
    expect(screen.getByRole('status')).toHaveTextContent('正在读取插件');
    expect(screen.queryByText('没有匹配的插件。')).not.toBeInTheDocument();
  });
});

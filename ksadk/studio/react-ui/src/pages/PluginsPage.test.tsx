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

  it("renders only DSH default and Codex compatibility inventories", async () => {
    mockedFetch.mockImplementation(async input => String(input).includes("/dsh/")
      ? response({ host: { available: true, version: "0.1.2" }, items: [{ ecosystem: "dsh", pluginId: "@deepseek-ai/demo", resolvedVersion: "1.0.0", state: "enabled" }] })
      : response({ host: { available: true, version: "0.148.0" }, items: [] }));
    render(<PluginsPage/>);
    expect(await screen.findAllByText("Demo")).toHaveLength(2);
    expect(screen.getByText("@deepseek-ai/demo")).toBeInTheDocument();
    expect(screen.queryByText("KsADK 原生插件")).not.toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Codex 插件" })).toHaveAttribute("aria-selected", "true");
    await userEvent.click(screen.getByRole("tab", { name: "DeepSeek Harness 插件" }));
    expect(screen.getByText("默认插件格式 · 当前 DSH Profile")).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "DeepSeek Harness 插件" })).toHaveAttribute("aria-selected", "true");
  });

  it("shows a loading state instead of a false empty Codex catalog", async () => {
    mockedFetch.mockImplementation(async () => await new Promise<Response>(() => {}));

    render(<PluginsPage/>);

    expect(screen.getByText("正在读取 Codex 插件目录…")).toBeInTheDocument();
    expect(screen.queryByText("当前没有可安装的 Codex 插件。")).not.toBeInTheDocument();
  });

  it("switches between Codex discovery and DSH source installation without page scrolling", async () => {
    mockedFetch.mockImplementation(async input => String(input).includes("/dsh/")
      ? response({ host: { available: true }, items: [] })
      : response({
          host: { available: true },
          items: [
            { ecosystem: "codex", pluginId: "gmail", displayName: "Gmail", installed: false },
            { ecosystem: "codex", pluginId: "figma", displayName: "Figma", installed: false },
          ],
        }));

    render(<PluginsPage/>);

    const search = await screen.findByRole("textbox", { name: "搜索插件" });
    await userEvent.type(search, "gmail");
    expect(screen.getByText("Gmail")).toBeInTheDocument();
    expect(screen.queryByText("Figma")).not.toBeInTheDocument();

    await userEvent.click(screen.getByRole("tab", { name: "DeepSeek Harness 插件" }));
    expect(screen.getByLabelText("DSH 插件来源")).toBeInTheDocument();
    expect(screen.queryByRole("textbox", { name: "搜索插件" })).not.toBeInTheDocument();
  });

  it("uses a product identity for the bundled Codex provider instead of its package name", async () => {
    mockedFetch.mockImplementation(async input => String(input).includes("/dsh/")
      ? response({ host: { available: true }, items: [{
          ecosystem: "dsh", pluginId: "@kingsoftcloud/ksadk-codex-provider", displayName: "@kingsoftcloud/ksadk-codex-provider",
          enabled: true, runtimeState: { state: "bound", providerRef: "plugin://io.ksadk.codex-provider@1.0.0" },
        }] })
      : response({ host: { available: true }, items: [] }));

    render(<PluginsPage/>);

    expect(await screen.findAllByText("Codex AgentProvider")).toHaveLength(2);
    expect(screen.getAllByText("KsADK 官方 · Agent Provider")).toHaveLength(2);
    expect(screen.getAllByText("让 Agent 使用 Codex App Server 的原生会话、工具与审批能力。")).toHaveLength(2);
    expect(screen.getByText("@kingsoftcloud/ksadk-codex-provider")).toBeInTheDocument();
  });

  it("shows concise five-state labels without treating enabled as ready", async () => {
    mockedFetch.mockImplementation(async input => String(input).includes("/dsh/")
      ? response({
          host: { available: true },
          items: [
            { ecosystem: "dsh", pluginId: "installed", enabled: false },
            { ecosystem: "dsh", pluginId: "enabled", enabled: true },
            { ecosystem: "dsh", pluginId: "ready", enabled: true, runtimeState: { state: "ready", providerRef: "plugin://ready" } },
            { ecosystem: "dsh", pluginId: "bound", enabled: true, runtimeState: { state: "bound", providerRef: "plugin://bound" } },
            { ecosystem: "dsh", pluginId: "failed", enabled: true, runtimeState: { state: "failed", errorCode: "BROKEN" } },
          ],
        })
      : response({ host: { available: true }, items: [] }));

    render(<PluginsPage/>);

    expect(await screen.findAllByText("待启用")).toHaveLength(2);
    expect(screen.getByText("已启用")).toBeInTheDocument();
    expect(screen.getByText("就绪")).toBeInTheDocument();
    expect(screen.getByText("已绑定")).toBeInTheDocument();
    expect(screen.getByText("失败")).toBeInTheDocument();
  });

  it("explains how a ready AgentProvider is used and exposes its real providerRef", async () => {
    mockedFetch.mockImplementation(async input => String(input).includes("/dsh/")
      ? response({
          host: { available: true },
          items: [{
            ecosystem: "dsh",
            pluginId: "provider",
            enabled: true,
            runtimeState: { state: "ready", providerRef: "plugin://demo-provider@1" },
          }],
        })
      : response({ host: { available: true }, items: [] }));

    render(<PluginsPage/>);

    expect(await screen.findByText("plugin://demo-provider@1")).toBeInTheDocument();
    expect(screen.getByText("在创建或编辑 Agent 时从 Runtime 选择器使用。")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "去创建 Agent" })).toHaveAttribute("href", "#/create");
  });

  it("does not prefill a subagent package as if it were a top-level Runtime", async () => {
    mockedFetch.mockResolvedValue(response({ host: { available: true }, items: [] }));
    render(<PluginsPage/>);
    await userEvent.click(await screen.findByRole("tab", { name: "DeepSeek Harness 插件" }));
    const source = await screen.findByLabelText("DSH 插件来源");
    expect(source).toHaveValue("");
    expect(source).toHaveAttribute("placeholder", expect.stringContaining("npm 包"));
    expect(source).not.toHaveValue("@deepseek-ai/dsh-subagent-codex");
  });

  it("keeps a newly installed DSH plugin pending explicit enablement", async () => {
    mockedFetch.mockImplementation(async input => {
      const path = String(input);
      if (path.endsWith("/plugins:install")) return response({ item: { ecosystem: "dsh", pluginId: "demo", installed: true, enabled: false } });
      if (path.includes("/dsh/")) return response({ host: { available: true }, items: [] });
      return response({ host: { available: true }, items: [] });
    });
    render(<PluginsPage/>);
    await userEvent.click(await screen.findByRole("tab", { name: "DeepSeek Harness 插件" }));
    await screen.findByLabelText("DSH 插件来源");
    await userEvent.type(screen.getByLabelText("DSH 插件来源"), "@example/demo@1.0.0");
    await userEvent.click(screen.getByText(/DSH 包及安装脚本/));
    await userEvent.click(screen.getByRole("button", { name: "安装到 Profile" }));

    await waitFor(() => expect(mockedToast).toHaveBeenCalledWith("已安装，待启用", "demo"));
  });

  it("keeps the Codex marketplace install flow after removing the third ecosystem", async () => {
    mockedFetch.mockImplementation(async (input, init) => {
      const path = String(input);
      if (path.includes("/dsh/")) return response({ host: { available: true }, items: [] });
      if (path.endsWith(":install")) return response({ item: { ecosystem: "codex", pluginId: "review", installed: true, state: "enabled" } });
      return response({
        host: { available: true, version: "0.148.0" },
        items: [{ ecosystem: "codex", pluginId: "review", marketplaceName: "official", installed: false, state: "disabled" }],
      });
    });

    render(<PluginsPage/>);
    expect(await screen.findByRole("button", { name: "安装" })).toBeDisabled();
    await userEvent.click(screen.getByText(/Codex 插件由 App Server/));
    await userEvent.click(screen.getByRole("button", { name: "安装" }));

    await waitFor(() => expect(mockedFetch).toHaveBeenCalledWith(
      "/api/v1/plugin-ecosystems/codex/plugins/review:install",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ marketplaceName: "official", acceptUndeclaredPermissions: true }),
      }),
    ));
  });
});

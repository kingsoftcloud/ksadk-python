import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { apiFetch } from "../api";
import { SettingsPage } from "./SettingsPage";

vi.mock("../api", () => ({ apiFetch: vi.fn() }));
const fetchMock = vi.mocked(apiFetch);
beforeEach(() => {
  fetchMock.mockReset();
  fetchMock.mockImplementation(async (path) => ({
    ok: true,
    json: async () => String(path).includes("system/settings")
      ? { sandbox: "workspace-write-auto", buildAfterCreate: true, codexProxy: "auto", cloudRegion: "test-region", cloudBucket: "" }
      : { items: [], workspace: { name: "test-workspace" } },
  }) as Response);
});

describe("Settings page category navigation", () => {
  it("shows one category and retains edits across categories before saving", async () => {
    const user = userEvent.setup();
    render(<SettingsPage themePreference="light" onThemePreferenceChange={vi.fn()} initialSection="cloud" />);
    // 云端/运行时分区各自持有保存按钮；设置加载完成后才可点。
    await waitFor(() => expect(screen.getByRole("button", { name: "保存" })).toBeEnabled());
    expect(screen.queryByRole("radiogroup", { name: "颜色模式" })).not.toBeInTheDocument();
    await user.clear(screen.getByLabelText("Region"));
    await user.type(screen.getByLabelText("Region"), "another-region");
    await user.click(screen.getByRole("button", { name: "通用" }));
    expect(screen.getByRole("radiogroup", { name: "颜色模式" })).toBeVisible();
    expect(screen.getByLabelText("Region")).not.toBeVisible();
    await user.click(screen.getByRole("button", { name: "云端连接" }));
    expect(screen.getByLabelText("Region")).toHaveValue("another-region");
    await user.click(screen.getByRole("button", { name: "保存" }));
    await waitFor(() => {
      const request = fetchMock.mock.calls.find(([, init]) => init?.method === "PUT");
      expect(request).toBeDefined();
    });
    const request = fetchMock.mock.calls.find(([, init]) => init?.method === "PUT");
    expect(JSON.parse(String(request?.[1]?.body))).toMatchObject({ cloudRegion: "another-region", sandbox: "workspace-write-auto" });
  });

  it("opens the requested category and groups runtime controls together", async () => {
    const user = userEvent.setup();
    render(<SettingsPage themePreference="light" onThemePreferenceChange={vi.fn()} initialSection="runtime" />);
    expect(screen.getByRole("button", { name: "运行与沙箱" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.queryByRole("radiogroup", { name: "颜色模式" })).not.toBeInTheDocument();
    expect(screen.getByRole("combobox", { name: "默认执行权限" })).toBeVisible();
    expect(screen.getByRole("combobox", { name: "Codex Responses 代理" })).toBeVisible();
  });

  it("defaults to the model category with the minimal setup form", async () => {
    render(<SettingsPage themePreference="light" onThemePreferenceChange={vi.fn()} />);
    await waitFor(() => expect(screen.getByRole("button", { name: "模型" })).toHaveAttribute("aria-pressed", "true"));
    expect(screen.getByLabelText(/^API Key/, { selector: "#setupApiKey" })).toBeVisible();
    expect(screen.getByLabelText(/接口地址/, { selector: "input" })).toBeVisible();
  });
});

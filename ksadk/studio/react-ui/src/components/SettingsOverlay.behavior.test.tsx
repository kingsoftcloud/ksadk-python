import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { apiFetch } from "../api";
import { SettingsOverlay } from "./SettingsOverlay";

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

describe("Settings category navigation", () => {
  it("shows one category and retains edits across categories before saving", async () => {
    const user = userEvent.setup();
    const onClose = vi.fn();
    render(<SettingsOverlay themePreference="light" onThemePreferenceChange={vi.fn()} onClose={onClose} />);
    await waitFor(() => expect(screen.getByRole("button", { name: "保存" })).toBeEnabled());
    expect(screen.getByRole("radiogroup", { name: "颜色模式" })).toBeVisible();
    expect(screen.queryByRole("combobox", { name: "默认执行权限" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "云端连接" }));
    await user.clear(screen.getByLabelText("Region"));
    await user.type(screen.getByLabelText("Region"), "another-region");
    await user.click(screen.getByRole("button", { name: "通用" }));
    expect(screen.getByLabelText("Region")).not.toBeVisible();
    await user.click(screen.getByRole("button", { name: "云端连接" }));
    expect(screen.getByLabelText("Region")).toHaveValue("another-region");
    await user.click(screen.getByRole("button", { name: "保存" }));
    await waitFor(() => expect(onClose).toHaveBeenCalledOnce());
    const request = fetchMock.mock.calls.find(([, init]) => init?.method === "PUT");
    expect(JSON.parse(String(request?.[1]?.body))).toMatchObject({ cloudRegion: "another-region", sandbox: "workspace-write-auto" });
  });

  it("opens the requested category and groups runtime controls together", async () => {
    const user = userEvent.setup();
    render(<SettingsOverlay initialSection="credentials" themePreference="light" onThemePreferenceChange={vi.fn()} onClose={vi.fn()} />);
    expect(screen.getByRole("button", { name: "模型与凭证" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.queryByRole("radiogroup", { name: "颜色模式" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "运行与沙箱" }));
    expect(screen.getByRole("combobox", { name: "默认执行权限" })).toBeVisible();
    expect(screen.getByRole("combobox", { name: "Codex Responses 代理" })).toBeVisible();
    expect(screen.getByRole("button", { name: "关闭" })).toBeVisible();
  });
});

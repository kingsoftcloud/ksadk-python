import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { apiFetch } from "../api";
import { NativePluginBindings, type NativePluginBinding } from "./NativePluginBindings";
vi.mock("../api", () => ({ apiFetch: vi.fn() }));
const fetchMock = vi.mocked(apiFetch);
const bound: NativePluginBinding = { ecosystem: "codex", pluginRef: "plugin://codex.other@1.0.0", snapshotDigest: `sha256:${"a".repeat(64)}`, components: ["skill:other"], enabled: true, config: { retained: true } };
const response = (data: unknown, ok = true) => ({ ok, json: async () => data }) as Response;
beforeEach(() => fetchMock.mockReset());
describe("native plugin bindings", () => {
  afterEach(async () => {
    // Selecting an item leaves the multi-select open. Wait for Radix's
    // deferred focus restoration before the test's jsdom realm is removed.
    const picker = screen.queryByRole("button", { name: "选择绑定插件" });
    if (picker?.getAttribute("aria-expanded") === "true") {
      await userEvent.keyboard("{Escape}");
      await waitFor(() => expect(picker).toHaveFocus());
    }
  });
  it("keeps existing bindings and exits the pending state when snapshot admission fails", async () => {
    fetchMock.mockImplementation(async (url, init) => {
      if (init?.method === "POST") return response({ error: { message: "快照校验失败" } }, false);
      if (String(url).includes("?")) return response({ items: [{ pluginId: "figma@official", displayName: "Figma", installed: true, enabled: true }] });
      return response({ snapshot: null });
    });
    const change = vi.fn();
    const pending = vi.fn();
    render(<NativePluginBindings value={[bound]} onChange={change} onPendingChange={pending} />);
    await userEvent.click(await screen.findByRole("button", { name: "选择绑定插件" }));
    await userEvent.click(await screen.findByRole("option", { name: /Figma/ }));
    expect(await screen.findByRole("alert")).toHaveTextContent("快照校验失败");
    expect(change).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: `移除 ${bound.pluginRef}` })).toBeInTheDocument();
    expect(pending.mock.calls).toEqual([[true], [false]]);
  });
  it("re-enables a pinned plugin without losing its selected components or duplicating the binding", async () => {
    fetchMock.mockImplementation(async url => String(url).includes("?")
      ? response({ items: [{ pluginId: "other@official", displayName: "Other", installed: true, enabled: true }] })
      : response({ snapshot: { ...bound, components: [{ id: "skill:other", kind: "skill" }, { id: "mcp:new-tool", kind: "mcp" }] } }));
    const change = vi.fn();
    render(<NativePluginBindings value={[{ ...bound, enabled: false }]} onChange={change} onPendingChange={vi.fn()} />);
    await userEvent.click(await screen.findByRole("button", { name: "选择绑定插件" }));
    await userEvent.click(await screen.findByRole("option", { name: /Other/ }));
    await waitFor(() => expect(change).toHaveBeenCalledWith([bound]));
  });
});

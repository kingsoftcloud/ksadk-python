import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { apiFetch } from "../api";
import { showToast } from "../components/Toast";
import { CreatePage } from "./CreatePage";

vi.mock("../api", () => ({ apiFetch: vi.fn() }));
vi.mock("../components/Toast", () => ({ showToast: vi.fn() }));
const workspace = "/fixture/workspace-a";
const key = `agentkit.studio.agentDraft.v2:${encodeURIComponent(workspace)}`;
let values: Map<string, string>;
let storage: Storage;
const model = { resourceId: "model-fixture", kind: "model", name: "fixture", displayName: "Fixture model", status: "ready", source: "local", version: "1.0.0" };
const tool = { ...model, resourceId: "tool-fixture", kind: "tool", displayName: "Fixture tool" };
const fields = {
  name: "Saved assistant", slug: "saved-assistant", runtimeType: "adk", template: "blank",
  prompt: "A saved requirement", description: "Saved description", audience: "研究人员", language: "zh-CN", depth: "deep", format: "report",
  systemPrompt: "Keep this edited system prompt", taskPrompt: "Keep this edited task", buildAfterCreate: false,
};
function savedDraft() {
  return { version: 2, workspacePath: workspace, savedAt: new Date().toISOString(), fields,
    wizard: { step: 3, maxStep: 4, selectedModels: [model.resourceId], selectedTools: [tool.resourceId], selectedMcp: [], selectedSkills: [],
      policy: "loose", contextOwnership: "framework", contextEngineRollout: "enabled", memoryEnabled: true,
      memoryWriteRollout: "shadow", selectedProviderRef: "", providerConfigText: "{}" } };
}
function renderPage(path = workspace) {
  return render(<CreatePage workspacePath={path} viewportMode="desktop" onBack={vi.fn()} onCreated={vi.fn()} />);
}
beforeEach(() => {
  vi.clearAllMocks();
  values = new Map();
  storage = {
    getItem: vi.fn(key => values.get(key) ?? null),
    setItem: vi.fn((key, value) => { values.set(key, value); }),
    removeItem: vi.fn(key => { values.delete(key); }), clear: vi.fn(() => values.clear()),
    key: vi.fn(index => [...values.keys()][index] ?? null), get length() { return values.size; },
  };
  Object.defineProperty(window, "localStorage", { configurable: true, value: storage });
  vi.mocked(apiFetch).mockImplementation(async (input, init) => {
    const path = String(input);
    const request = init?.body ? JSON.parse(String(init.body)) : {};
    const payload = path.includes("agent-templates/")
      ? { spec: { instructions: { system: "Template system", task: "Template task" }, bindings: { modelProfileIds: [model.resourceId], tools: (request.toolResourceIds || []).map((resourceId: string) => ({ resourceId })) } } }
      : { items: path.includes("catalog/resources") ? [model, tool] : [] };
    return { ok: true, json: async () => payload } as Response;
  });
});

describe("workspace-scoped quick drafts", () => {
  it("saves an incomplete form and restores it after mounting again", async () => {
    const user = userEvent.setup();
    const view = renderPage();
    await user.clear(screen.getByRole("textbox", { name: /Agent 名称/ }));
    await user.type(screen.getByRole("textbox", { name: /Agent 名称/ }), "Partial assistant");
    await user.click(screen.getByRole("button", { name: "保存草稿" }));
    expect(JSON.parse(values.get(key)!)).toMatchObject({ version: 2, workspacePath: workspace, fields: { name: "Partial assistant", prompt: "" } });
    expect(showToast).toHaveBeenCalledWith("草稿已保存", expect.any(String));
    view.unmount();
    renderPage();
    await waitFor(() => expect(screen.getByRole("textbox", { name: /Agent 名称/ })).toHaveValue("Partial assistant"));
    expect(screen.getByRole("textbox", { name: /Agent 目标与要求/ })).toHaveValue("");
  });

  it("restores the step, bindings and policy without overwriting edited prompts", async () => {
    values.set(key, JSON.stringify(savedDraft()));
    const user = userEvent.setup();
    renderPage();
    await waitFor(() => expect(screen.getByRole("textbox", { name: /角色与系统提示词/ })).toHaveValue(fields.systemPrompt));
    await waitFor(() => expect(apiFetch).toHaveBeenCalledWith("/api/v1/agent-templates/blank:compose", expect.anything()));
    expect(screen.getByRole("textbox", { name: "任务契约" })).toHaveValue(fields.taskPrompt);
    await user.click(screen.getByRole("button", { name: "上一步" }));
    expect(screen.getByRole("button", { name: "移除 Fixture model" })).toBeVisible();
    expect(screen.getByRole("button", { name: "移除 Fixture tool" })).toBeVisible();
    expect(screen.getByRole("button", { name: "宽松" })).toHaveClass("selected");
    await user.click(screen.getByRole("button", { name: "保存草稿" }));
    expect(JSON.parse(values.get(key)!)).toMatchObject({ fields, wizard: { step: 2, maxStep: 4, policy: "loose", contextOwnership: "framework", memoryEnabled: true } });
  });

  it("restores platform bindings and clears them when switching workspaces", async () => {
    const bindings = [{ ecosystem: "dsh", pluginRef: "plugin://fixture/knowledge@1.0.0",
      snapshotDigest: "fixture-digest", components: ["knowledge"], enabled: true,
      config: { knowledgeBaseId: "fixture-kb" } }];
    const draft = savedDraft();
    values.set(key, JSON.stringify({ ...draft, wizard: { ...draft.wizard, selectedPlatformResources: bindings } }));
    const view = renderPage();
    await waitFor(() => expect(screen.getByRole("textbox", { name: /角色与系统提示词/ })).toHaveValue(fields.systemPrompt));
    await userEvent.click(screen.getByRole("button", { name: "保存草稿" }));
    expect(JSON.parse(values.get(key)!).wizard.selectedPlatformResources).toEqual(bindings);
    const nextWorkspace = "/fixture/workspace-b";
    view.rerender(<CreatePage workspacePath={nextWorkspace} viewportMode="desktop" onBack={vi.fn()} onCreated={vi.fn()} />);
    await waitFor(() => expect(screen.getByRole("textbox", { name: /Agent 名称/ })).toHaveValue("New Agent"));
    await userEvent.click(screen.getByRole("button", { name: "保存草稿" }));
    const nextKey = `agentkit.studio.agentDraft.v2:${encodeURIComponent(nextWorkspace)}`;
    expect(JSON.parse(values.get(nextKey)!).wizard.selectedPlatformResources).toEqual([]);
    expect(JSON.parse(values.get(key)!).wizard.selectedPlatformResources).toEqual(bindings);
  });

  it("does not import legacy, mismatched or malformed draft records", async () => {
    values.set("agentkit.studio.agentDraft.v1:local-workspace", JSON.stringify(savedDraft()));
    values.set(key, JSON.stringify({ ...savedDraft(), workspacePath: "/fixture/other" }));
    const view = renderPage();
    await waitFor(() => expect(showToast).toHaveBeenCalledWith("未恢复草稿", expect.any(String), "error"));
    expect(screen.getByRole("textbox", { name: /Agent 名称/ })).toHaveValue("New Agent");
    view.unmount();
    values.set(key, "broken json");
    renderPage();
    await waitFor(() => expect(showToast).toHaveBeenCalledWith("未恢复草稿", "无法读取本地草稿，请重新填写。", "error"));
    expect(screen.getByRole("textbox", { name: /Agent 名称/ })).toHaveValue("New Agent");
  });

  it("reports a storage write failure without claiming the draft was saved", async () => {
    vi.mocked(storage.setItem).mockImplementation(() => { throw new Error("Storage denied"); });
    renderPage();
    await userEvent.click(screen.getByRole("button", { name: "保存草稿" }));
    expect(showToast).toHaveBeenCalledWith("草稿保存失败", "Storage denied", "error");
    expect(showToast).not.toHaveBeenCalledWith("草稿已保存", expect.anything());
    expect(values.size).toBe(0);
  });

  it("rejects plaintext credentials in a restored provider configuration", async () => {
    const draft = savedDraft();
    draft.wizard.providerConfigText = JSON.stringify({ apiKey: "fixture-plaintext-value" });
    values.set(key, JSON.stringify(draft));
    renderPage();
    await waitFor(() => expect(showToast).toHaveBeenCalledWith("未恢复草稿", expect.any(String), "error"));
    expect(screen.getByRole("textbox", { name: /Agent 名称/ })).toHaveValue("New Agent");
  });
});

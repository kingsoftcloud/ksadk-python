import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
import { apiFetch } from "../api";
import { RemoteAgentBindingsEditor, type RemoteAgentBinding } from "./RemoteAgentBindingsEditor";
vi.mock("../api", () => ({ apiFetch: vi.fn() }));
const binding: RemoteAgentBinding = { bindingId: "binding-1", spaceId: "space-1", targetAgentId: "agent-1", expectedVersionId: "v1", localName: "reviewer", exposure: "auto", policyRef: "policy://a2a/runtime", digest: "sha256:fixed" };
const response = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status });
function catalog(path: string) {
  if (path.includes("/versions")) return response({ items: [{ versionId: "v1", agentVersion: "current", sequence: 2 }, { versionId: "old", agentVersion: "old", sequence: 1 }], total: 2 });
  if (path.endsWith("/agents")) return response({ items: [{ targetAgentId: "agent-1", name: "Reviewer", versionId: "v1" }] });
  return response({ items: [{ spaceId: "space-1", name: "Team", status: "available" }, { spaceId: "space-2", name: "Other", status: "available" }], total: 2 });
}
async function choose() {
  fireEvent.click(screen.getByText(/远端 A2A Agent/, { selector: "summary" }));
  await screen.findByRole("option", { name: "Team (space-1)" });
  fireEvent.change(screen.getByLabelText("A2A Space"), { target: { value: "space-1" } });
  await screen.findByRole("option", { name: "Reviewer (agent-1)" });
  fireEvent.change(screen.getByLabelText("远端 Agent"), { target: { value: "agent-1" } });
  await screen.findByRole("option", { name: "当前可调用版本 (v1)" });
  fireEvent.change(screen.getByLabelText("固定版本"), { target: { value: "v1" } });
  fireEvent.change(screen.getByLabelText("调用名称"), { target: { value: "reviewer" } });
  fireEvent.change(screen.getByLabelText("策略引用"), { target: { value: "policy://a2a/runtime" } });
}
beforeEach(() => {
  vi.resetAllMocks();
  vi.mocked(apiFetch).mockImplementation(async path => String(path).endsWith("bindings:freeze") ? response(binding) : catalog(String(path)));
});
it("freezes the current version via BFF, preserving the server digest", async () => {
  const change = vi.fn();
  render(<RemoteAgentBindingsEditor value={[]} onChange={change} />);
  expect(apiFetch).not.toHaveBeenCalled();
  await choose();
  expect(screen.queryByRole("option", { name: "old (old)" })).not.toBeInTheDocument();
  expect(vi.mocked(apiFetch).mock.calls.some(([path]) => String(path).includes("/versions"))).toBe(false);
  fireEvent.click(screen.getByRole("button", { name: "固定此绑定" }));
  await waitFor(() => expect(change).toHaveBeenCalledWith([binding]));
  const call = vi.mocked(apiFetch).mock.calls.find(([path]) => String(path).endsWith("bindings:freeze"))!;
  expect(JSON.parse(String(call[1]?.body))).toEqual({ spaceId: "space-1", targetAgentId: "agent-1", expectedVersionId: "v1", localName: "reviewer", exposure: "auto", policyRef: "policy://a2a/runtime" });
});
it("preserves existing bindings on drift without displaying upstream secrets", async () => {
  vi.mocked(apiFetch).mockImplementation(async path => String(path).endsWith("bindings:freeze") ? response({ error: "secret-internal-route" }, 409) : catalog(String(path)));
  const change = vi.fn();
  render(<RemoteAgentBindingsEditor value={[{ ...binding, bindingId: "prior", targetAgentId: "prior-agent", localName: "prior" }]} onChange={change} />);
  await choose();
  fireEvent.click(screen.getByRole("button", { name: "固定此绑定" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("绑定已变化");
  expect(change).not.toHaveBeenCalled();
  expect(screen.queryByText(/secret-internal-route/)).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "移除 prior" })).toBeEnabled();
});
it("rejects duplicate targets with different local names before freeze", async () => {
  const change = vi.fn();
  render(<RemoteAgentBindingsEditor value={[binding]} onChange={change} />);
  await choose();
  fireEvent.change(screen.getByLabelText("调用名称"), { target: { value: "another" } });
  fireEvent.click(screen.getByRole("button", { name: "固定此绑定" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("已绑定");
  expect(change).not.toHaveBeenCalled();
  expect(vi.mocked(apiFetch).mock.calls.some(([path]) => String(path).endsWith("bindings:freeze"))).toBe(false);
});
it("discards a stale member response after changing Space", async () => {
  let release: (response: Response) => void = () => {};
  vi.mocked(apiFetch).mockImplementation(async path => {
    if (String(path).includes("space-1/agents")) return new Promise<Response>(resolve => { release = resolve; });
    if (String(path).includes("space-2/agents")) return response({ items: [] });
    return catalog(String(path));
  });
  render(<RemoteAgentBindingsEditor value={[]} onChange={vi.fn()} />);
  fireEvent.click(screen.getByText(/远端 A2A Agent/, { selector: "summary" }));
  await screen.findByRole("option", { name: "Team (space-1)" });
  fireEvent.change(screen.getByLabelText("A2A Space"), { target: { value: "space-1" } });
  await waitFor(() => expect(apiFetch).toHaveBeenCalledWith("/api/v1/a2a/spaces/space-1/agents", undefined));
  fireEvent.change(screen.getByLabelText("A2A Space"), { target: { value: "space-2" } });
  release(catalog("/space-1/agents"));
  await waitFor(() => expect(apiFetch).toHaveBeenCalledWith("/api/v1/a2a/spaces/space-2/agents", undefined));
  expect(screen.queryByRole("option", { name: "Reviewer (agent-1)" })).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "固定此绑定" })).toBeDisabled();
});

it("offers only executable policies and defaults to the session profile", () => {
  render(<RemoteAgentBindingsEditor value={[]} onChange={vi.fn()} />);
  fireEvent.click(screen.getByText(/远端 A2A Agent/, { selector: "summary" }));
  const policy = screen.getByLabelText("策略引用");
  expect(policy).toHaveValue("policy://a2a/runtime");
  expect(Array.from((policy as HTMLSelectElement).options).map(option => option.value)).toEqual(["policy://a2a/runtime", "policy://a2a/strict"]);
});

it("refreshes the catalog and clears the selected callable version before rebinding", async () => {
  const change = vi.fn();
  render(<RemoteAgentBindingsEditor value={[]} onChange={change} />);
  await choose();
  const catalogReads = () => vi.mocked(apiFetch).mock.calls.filter(([path]) => String(path).includes("spaces?page=")).length;
  const before = catalogReads();
  expect(screen.getByRole("button", { name: "固定此绑定" })).toBeEnabled();
  fireEvent.click(screen.getByRole("button", { name: "刷新目录" }));
  await waitFor(() => expect(catalogReads()).toBe(before + 1));
  expect(screen.getByLabelText("A2A Space")).toHaveValue("");
  expect(screen.getByLabelText("远端 Agent")).toHaveValue("");
  expect(screen.getByLabelText("固定版本")).toHaveValue("");
  expect(screen.getByRole("button", { name: "固定此绑定" })).toBeDisabled();
  expect(change).not.toHaveBeenCalled();
  fireEvent.change(screen.getByLabelText("A2A Space"), { target: { value: "space-1" } });
  await screen.findByRole("option", { name: "Reviewer (agent-1)" });
  fireEvent.change(screen.getByLabelText("远端 Agent"), { target: { value: "agent-1" } });
  fireEvent.change(screen.getByLabelText("固定版本"), { target: { value: "v1" } });
  fireEvent.click(screen.getByRole("button", { name: "固定此绑定" }));
  await waitFor(() => expect(change).toHaveBeenCalledWith([binding]));
});

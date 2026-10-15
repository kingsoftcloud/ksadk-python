import { useState } from "react";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
import { apiFetch } from "../api";
import { A2AOrchestrationEditor, type A2AOrchestration } from "./A2AOrchestrationEditor";
vi.mock("../api", () => ({ apiFetch: vi.fn() }));
vi.mock("./ui/CodeViewer", () => ({ CodeViewer: ({ code }: { code: string }) => <pre>{code}</pre> }));
const binding = { bindingId: "binding-1", spaceId: "space-1", targetAgentId: "agent-1", expectedVersionId: "version-1", localName: "expert", exposure: "auto" as const, policyRef: "policy://a2a/runtime", digest: "sha256:fixed" };
const runtime = { type: "langgraph", projectPath: ".", entryPoint: "agent.py", agentVariable: "root_agent" };
const initial: A2AOrchestration = { entryNodeId: "local", nodes: [{ nodeId: "local", kind: "local" }, { nodeId: "remote", kind: "remote", bindingId: binding.bindingId }], edges: [{ source: "local", target: "remote", kind: "sequential" }] };
const preview = { plan: { sequence: ["local", "remote"], handoffs: [] }, source: "remote_node(spec)", entrySource: "root_agent = compose()", digest: "sha256:preview" };
beforeEach(() => { vi.resetAllMocks(); vi.mocked(apiFetch).mockResolvedValue(new Response(JSON.stringify(preview))); });
function Wrapper({ start = null }: { start?: A2AOrchestration | null }) {
  const [value, setValue] = useState(start);
  return <A2AOrchestrationEditor agentId="draft-1" revision={3} runtime={runtime} bindings={[binding]} value={value} onChange={setValue} />;
}
function open() { fireEvent.click(screen.getByText(/A2A 编排/, { selector: "summary" })); }
it("authors a sequential graph and previews the exact unsaved declaration without saving", async () => {
  render(<Wrapper />); open();
  fireEvent.click(screen.getByRole("button", { name: "启用 A2A 编排" }));
  fireEvent.click(screen.getByRole("button", { name: "添加远端节点" }));
  fireEvent.click(screen.getByRole("button", { name: "添加连线" }));
  fireEvent.change(screen.getByLabelText("起点 1"), { target: { value: "local" } });
  fireEvent.change(screen.getByLabelText("终点 1"), { target: { value: "remote_1" } });
  fireEvent.click(screen.getByRole("button", { name: "预览当前编排" }));
  await screen.findByText("remote_node(spec)");
  expect(apiFetch).toHaveBeenCalledTimes(1);
  const [path, options] = vi.mocked(apiFetch).mock.calls[0];
  expect(path).toBe("/api/v1/agents/draft-1/a2a:preview");
  expect(options?.method).toBe("POST");
  expect(JSON.parse(String(options?.body))).toMatchObject({ expectedRevision: 3, runtime, bindings: [binding], orchestration: { edges: [{ source: "local", target: "remote_1", kind: "sequential" }] } });
  expect(screen.queryByRole("option", { name: /parallel|conditional|loop/i })).not.toBeInTheDocument();
});
it("renames references, invalidates preview, and removes edges with their node", async () => {
  render(<Wrapper start={initial} />); open();
  fireEvent.click(screen.getByRole("button", { name: "预览当前编排" }));
  await screen.findByText("remote_node(spec)");
  fireEvent.change(screen.getByLabelText("节点名称 1"), { target: { value: "supervisor" } });
  expect(screen.getByLabelText("入口节点")).toHaveValue("supervisor");
  expect(screen.getByLabelText("起点 1")).toHaveValue("supervisor");
  expect(screen.queryByText("remote_node(spec)")).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "移除节点 2" }));
  expect(screen.queryByLabelText("起点 1")).not.toBeInTheDocument();
});
it("shows a safe drift error without exposing upstream data", async () => {
  vi.mocked(apiFetch).mockResolvedValue(new Response(JSON.stringify({ error: "private-route-and-token" }), { status: 409 }));
  render(<Wrapper start={initial} />); open();
  fireEvent.click(screen.getByRole("button", { name: "预览当前编排" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("版本已变化");
  expect(screen.queryByText(/private-route/)).not.toBeInTheDocument();
  expect(screen.queryByLabelText("编排预览")).not.toBeInTheDocument();
});
it("does not publish a stale preview after a binding change", async () => {
  let release: (value: Response) => void = () => {};
  vi.mocked(apiFetch).mockReturnValue(new Promise(resolve => { release = resolve; }));
  const props = { agentId: "draft-1", revision: 3, runtime, bindings: [binding], value: initial, onChange: vi.fn() };
  const { rerender } = render(<A2AOrchestrationEditor {...props} />); open();
  fireEvent.click(screen.getByRole("button", { name: "预览当前编排" }));
  rerender(<A2AOrchestrationEditor {...props} bindings={[]} />);
  release(new Response(JSON.stringify(preview)));
  await waitFor(() => expect(screen.queryByText("正在校验绑定并编译…")).not.toBeInTheDocument());
  expect(screen.queryByText("remote_node(spec)")).not.toBeInTheDocument();
});

it("allows ADK fixed-target handoff in the editor", () => {
  render(<A2AOrchestrationEditor agentId="draft-1" revision={3} runtime={{ ...runtime, type: "adk" }} bindings={[binding]} value={initial} onChange={vi.fn()} />);
  open();
  expect(screen.getByRole("option", { name: "Handoff 工具" })).toBeEnabled();
});

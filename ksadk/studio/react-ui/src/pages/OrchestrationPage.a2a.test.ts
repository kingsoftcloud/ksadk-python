import { expect, it } from "vitest";
import { layoutA2AOrchestration } from "./OrchestrationPage";

it("renders only declared A2A edges with stable node/binding identity", () => {
  const plan = { entryNodeId: "supervisor", nodes: [
    { nodeId: "supervisor", kind: "local" as const },
    { nodeId: "reviewer", kind: "remote" as const, bindingId: "binding-review" },
    { nodeId: "writer", kind: "local" as const },
  ], edges: [{ source: "supervisor", target: "reviewer", kind: "handoff" as const },
    { source: "supervisor", target: "writer", kind: "sequential" as const }] };
  const binding = { bindingId: "binding-review", localName: "expert", spaceId: "space", targetAgentId: "agent", expectedVersionId: "version-1", exposure: "auto" as const, policyRef: "policy://a2a/runtime", digest: "sha256:fixture" };
  const graph = layoutA2AOrchestration(plan, [binding], 800);
  expect(graph.nodes.map(node => node.id)).toEqual(["supervisor", "reviewer", "writer"]);
  expect(graph.nodes[1].data).toMatchObject({ bindingId: "binding-review", designNodeId: "reviewer", subtitle: "expert · version-1" });
  expect(graph.edges.map(edge => [edge.source, edge.target, edge.label])).toEqual([
    ["supervisor", "reviewer", "Handoff 工具"], ["supervisor", "writer", "顺序"],
  ]);
  expect(graph.edges.some(edge => edge.source === "reviewer" && edge.target === "writer")).toBe(false);
  expect(layoutA2AOrchestration(plan, [], 400).nodes[1].data.subtitle).toBe("远端绑定已缺失");
});

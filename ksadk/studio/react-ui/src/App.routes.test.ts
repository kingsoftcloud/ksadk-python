import { describe, expect, it } from "vitest";
import {
  parseChatTargetValue,
  parseStudioLocationHash,
} from "./App";

describe("Studio route parsing", () => {
  it("preserves Agent detail, edit and resource deep links", () => {
    expect(parseStudioLocationHash("#/agents/demo-agent")).toMatchObject({
      view: "agent-detail",
      detailAgentId: "demo-agent",
    });
    expect(parseStudioLocationHash("#/agents/demo-agent/edit")).toMatchObject({
      view: "create",
      editingAgentId: "demo-agent",
    });
    expect(parseStudioLocationHash("#/resources/skill")).toMatchObject({
      view: "resources",
      resourceKind: "skill",
    });
    expect(parseStudioLocationHash("#/deployments/new?buildId=build-1&agentId=demo-agent")).toMatchObject({
      view: "deployments",
    });
    expect(parseStudioLocationHash("#/plugins")).toMatchObject({
      view: "plugins",
    });
  });

  it("falls back from retired in-process extension routes", () => {
    expect(parseStudioLocationHash("#/extensions/tasks")).toMatchObject({ view: "agents" });
  });

  it("preserves an account-scoped CLI Agent id when selecting a cloud chat target", () => {
    expect(parseChatTargetValue("cloud:account:ar-20260820153835-94c90b9b")).toEqual({
      kind: "cloud",
      id: "account:ar-20260820153835-94c90b9b",
    });
  });
});

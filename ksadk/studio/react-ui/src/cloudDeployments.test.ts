import { describe, expect, it } from "vitest";

import { selectCloudChatDeployments } from "./cloudDeployments";

describe("selectCloudChatDeployments", () => {
  it("shows one target per agent and prefers its READY receipt", () => {
    expect(selectCloudChatDeployments([
      { id: "dep-failed", agentId: "agent-1", status: "FAILED" },
      { id: "dep-ready", agentId: "agent-1", status: "READY" },
      { id: "dep-other", agentId: "agent-2", status: "DEPLOYING" },
    ])).toEqual([
      { id: "dep-ready", agentId: "agent-1", status: "READY" },
      { id: "dep-other", agentId: "agent-2", status: "DEPLOYING" },
    ]);
  });

  it("ignores unbound receipts and keeps the first equal-status receipt", () => {
    expect(selectCloudChatDeployments([
      { id: "dep-first", agentId: "agent-1", status: "READY" },
      { id: "dep-second", agentId: "agent-1", status: "READY" },
      { id: "dep-unbound", status: "READY" },
    ])).toEqual([
      { id: "dep-first", agentId: "agent-1", status: "READY" },
    ]);
  });
});

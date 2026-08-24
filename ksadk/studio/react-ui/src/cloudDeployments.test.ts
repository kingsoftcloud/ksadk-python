import { describe, expect, it } from "vitest";

import {
  mergeCloudChatTargets,
  resolveCloudChatRoute,
  selectCloudChatDeployments,
} from "./cloudDeployments";

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

  it("prefers the receipt matching the live managed version before stale status", () => {
    expect(selectCloudChatDeployments([
      { id: "dep-old", agentId: "agent-1", status: "READY", versionId: "managed-old" },
      { id: "dep-current", agentId: "agent-1", status: "FAILED", versionId: "managed-current" },
    ], new Map([["agent-1", "managed-current"]]))).toEqual([
      { id: "dep-current", agentId: "agent-1", status: "FAILED", versionId: "managed-current" },
    ]);
  });
});

describe("mergeCloudChatTargets", () => {
  it("keeps receipt provenance while merging account metadata and adding account-only Agents", () => {
    expect(mergeCloudChatTargets(
      [{ id: "dep-ready", agentId: "agent-1", status: "READY", endpoint: "stale" }],
      [
        {
          agentId: "agent-1", name: "Managed Agent", status: "RUNNING",
          endpoint: "live", framework: "codex", versionId: "version-2",
          updatedAt: "2026-08-24T10:00:00Z",
        },
        { agentId: "agent-2", name: "Existing Code", status: "RUNNING", framework: "langgraph" },
      ],
    )).toEqual([
      {
        id: "dep-ready", agentId: "agent-1", agentName: "Managed Agent",
        status: "RUNNING", endpoint: "live", framework: "codex",
        versionId: "version-2", updatedAt: "2026-08-24T10:00:00Z",
        source: "receipt",
      },
      {
        id: "account:agent-2",
        agentId: "agent-2",
        agentName: "Existing Code",
        status: "RUNNING",
        endpoint: undefined,
        framework: "langgraph",
        versionId: undefined,
        updatedAt: undefined,
        source: "account",
      },
    ]);
  });

  it("binds chat to the receipt whose version matches the live Agent", () => {
    expect(mergeCloudChatTargets(
      [
        { id: "dep-old", agentId: "agent-1", status: "READY", versionId: "managed-old" },
        { id: "dep-current", agentId: "agent-1", status: "FAILED", versionId: "managed-current" },
      ],
      [{ agentId: "agent-1", name: "Managed Agent", status: "RUNNING", versionId: "managed-current" }],
    )[0]).toMatchObject({
      id: "dep-current",
      status: "RUNNING",
      versionId: "managed-current",
      source: "receipt",
    });
  });
});

describe("resolveCloudChatRoute", () => {
  it.each(["hermes", "openclaw"])(
    "routes %s without a declared SessionEvent chat capability to its official dashboard",
    framework => {
      expect(resolveCloudChatRoute({ framework })).toEqual({
        kind: "official-dashboard",
        reason: "native-runtime-without-session-event-chat-capability",
      });
    },
  );

  it("keeps Codex on the Studio SessionEvent transport", () => {
    expect(resolveCloudChatRoute({ framework: "codex" })).toEqual({
      kind: "studio-session-events",
      reason: "studio-compatible-framework",
    });
  });

  it("honours an explicit SessionEvent chat capability before framework fallback", () => {
    expect(resolveCloudChatRoute({
      framework: "hermes",
      capabilities: { sessionEventChat: { enabled: true } },
    })).toEqual({
      kind: "studio-session-events",
      reason: "declared-session-event-chat-capability",
    });
  });
});

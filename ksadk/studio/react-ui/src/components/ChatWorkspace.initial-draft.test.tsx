import { act, fireEvent, render, screen } from "@testing-library/react";
import type { ConversationController } from "@kingsoftcloud/ksadk-web/conversation";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ChatWorkspace } from "./ChatWorkspace";

const passiveEffects = vi.hoisted(() => ({
  paused: true,
  pending: [] as Array<{ run: () => void }>,
}));
const bootstrap = vi.hoisted(() => ({
  status: "ready", agentId: undefined as string | undefined,
  sessionId: null as string | null, starts: 0,
}));

// The browser may accept input after paint and before passive effects run.
// Keep that interval deterministic while retaining React's real layout effects
// and the published Web composer/controller draft hydration.
vi.mock("react", async importOriginal => {
  const react = await importOriginal<typeof import("react")>();
  return {
    ...react,
    useEffect: (setup: React.EffectCallback, dependencies?: React.DependencyList) => {
      react.useEffect(() => {
        if (!passiveEffects.paused) return setup();
        let cancelled = false;
        let cleanup: ReturnType<React.EffectCallback>;
        passiveEffects.pending.push({ run: () => {
          if (!cancelled) cleanup = setup();
        } });
        return () => { cancelled = true; cleanup?.(); };
      }, dependencies);
    },
  };
});

vi.mock("@kingsoftcloud/ksadk-web/hooks", async () => {
  const { useCallback, useReducer } = await import("react");
  return { useAgentChat: ({ agentId, conversationController }: {
    agentId: string; conversationController: ConversationController;
  }) => {
    const [, rerender] = useReducer(revision => revision + 1, 0);
    const startNewConversation = useCallback(() => {
      conversationController.createDraft(agentId);
      bootstrap.sessionId = null;
      bootstrap.starts += 1;
      rerender();
    }, [agentId, conversationController]);
    const selectSession = useCallback(() => {}, []);
    const refresh = useCallback(async () => {}, []);
    return {
      agentId: bootstrap.agentId ?? agentId, bootstrapStatus: bootstrap.status, uiCapabilities: {},
      currentSessionId: bootstrap.sessionId, messages: [], sessions: [], pendingInteractions: [],
      conversationId: conversationController.getOrCreate(agentId, bootstrap.sessionId),
      conversationDrafts: conversationController.drafts,
      startNewConversation, selectSession, refresh,
      isOutboxRequestActive: () => false,
    };
  } };
});
vi.mock("@kingsoftcloud/ksadk-web/runtime", () => ({
  AgentEngineClient: class {},
  ApiFacadeImpl: class {},
}));
vi.mock("@kingsoftcloud/ksadk-web/chat/timeline", () => ({
  AgentConversationTimeline: () => null,
}));
vi.mock("../api", () => ({ apiFetch: vi.fn() }));
vi.mock("./AgentAvatar", () => ({ AgentAvatar: () => null }));

describe("ChatWorkspace initial draft ownership", () => {
  beforeEach(() => {
    localStorage.clear();
    bootstrap.status = "ready";
    bootstrap.agentId = undefined;
    bootstrap.sessionId = null;
    bootstrap.starts = 0;
    passiveEffects.paused = true;
    passiveEffects.pending.length = 0;
  });
  afterEach(() => {
    passiveEffects.paused = false;
    passiveEffects.pending.length = 0;
  });

  it("keeps the first editable frame's input when initialization effects settle", () => {
    render(<ChatWorkspace agentId="initial-draft-agent" agentName="Agent" />);
    const composer = screen.getByPlaceholderText("发送消息…");
    const send = screen.getByRole("button", { name: "发送消息" });
    fireEvent.change(composer, { target: { value: "检查一次" } });
    expect(composer).toHaveValue("检查一次");
    expect(send).toBeEnabled();

    act(() => {
      passiveEffects.paused = false;
      passiveEffects.pending.splice(0).forEach(effect => effect.run());
    });

    expect(composer).toHaveValue("检查一次");
    expect(send).toBeEnabled();
  });

  it("does not expose an editable composer before the Agent bootstrap resolves", () => {
    bootstrap.status = "loading";
    render(<ChatWorkspace agentId="initial-draft-agent" agentName="Agent" />);
    expect(screen.getByRole("status", { name: "正在连接 Agent" })).toBeVisible();
    expect(screen.queryByPlaceholderText("发送消息…")).not.toBeInTheDocument();
  });

  it("waits for the selected Agent instead of editing the previous Agent's draft", () => {
    const { rerender } = render(<ChatWorkspace agentId="agent-a" agentName="Agent A" />);
    fireEvent.change(screen.getByPlaceholderText("发送消息…"), { target: { value: "Agent A draft" } });
    bootstrap.agentId = "agent-a";
    rerender(<ChatWorkspace agentId="agent-b" agentName="Agent B" />);
    expect(screen.queryByPlaceholderText("发送消息…")).not.toBeInTheDocument();
    bootstrap.agentId = "agent-b";
    rerender(<ChatWorkspace agentId="agent-b" agentName="Agent B" />);
    expect(screen.getByPlaceholderText("发送消息…")).toHaveValue("");
    expect(bootstrap.starts).toBe(2);
  });

  it("keeps a stable scope's draft across path metadata and product navigation", () => {
    const { rerender } = render(<ChatWorkspace agentId="agent" agentName="Agent" workspaceId="workspace" workspacePath="/old" />);
    fireEvent.change(screen.getByPlaceholderText("发送消息…"), { target: { value: "same owner" } });
    rerender(<ChatWorkspace agentId="agent" agentName="Agent" workspaceId="workspace" workspacePath="/new" />);
    expect(screen.getByPlaceholderText("发送消息…")).toHaveValue("same owner");
    expect(bootstrap.starts).toBe(1);
    rerender(<ChatWorkspace agentId="agent" agentName="Agent" workspaceId="workspace" credentialScope="other" />);
    expect(screen.getByPlaceholderText("发送消息…")).toHaveValue("");
    fireEvent.change(screen.getByPlaceholderText("发送消息…"), { target: { value: "new owner" } });
    rerender(<ChatWorkspace agentId="agent" agentName="Agent" workspaceId="workspace" credentialScope="other" active={false} />);
    rerender(<ChatWorkspace agentId="agent" agentName="Agent" workspaceId="workspace" credentialScope="other" />);
    expect(screen.getByPlaceholderText("发送消息…")).toHaveValue("new owner");
    expect(bootstrap.starts).toBe(2);
  });

  it("waits for a cold explicit session selection before exposing its composer", async () => {
    bootstrap.sessionId = "restoring-old-session";
    const props = { agentId: "agent", agentName: "Agent", requestedSessionId: "requested-session" };
    const { rerender } = render(<ChatWorkspace {...props} />);
    expect(screen.queryByPlaceholderText("发送消息…")).not.toBeInTheDocument();
    bootstrap.sessionId = "requested-session";
    rerender(<ChatWorkspace {...props} />);
    await act(async () => {
      passiveEffects.paused = false;
      passiveEffects.pending.splice(0).forEach(effect => effect.run());
    });
    expect(screen.getByPlaceholderText("发送消息…")).toHaveValue("");
    expect(bootstrap.starts).toBe(0);
  });

  it("gates a new explicit session request and still permits new chat after confirmation", async () => {
    bootstrap.sessionId = "session-1";
    const props = { agentId: "agent", agentName: "Agent" };
    const { rerender } = render(<ChatWorkspace {...props} requestedSessionId="session-1" />);
    expect(screen.queryByPlaceholderText("发送消息…")).not.toBeInTheDocument();
    await act(async () => {
      passiveEffects.paused = false;
      passiveEffects.pending.splice(0).forEach(effect => effect.run());
    });
    fireEvent.change(screen.getByPlaceholderText("发送消息…"), { target: { value: "session 1 draft" } });
    rerender(<ChatWorkspace {...props} requestedSessionId="session-2" />);
    expect(screen.queryByPlaceholderText("发送消息…")).not.toBeInTheDocument();
    bootstrap.sessionId = "session-2";
    rerender(<ChatWorkspace {...props} requestedSessionId="session-2" />);
    await act(async () => {});
    expect(screen.getByPlaceholderText("发送消息…")).toHaveValue("");
    rerender(<ChatWorkspace {...props} requestedSessionId="session-2" newChatRequest={1} />);
    fireEvent.change(screen.getByPlaceholderText("发送消息…"), { target: { value: "fresh draft" } });
    expect(screen.getByPlaceholderText("发送消息…")).toHaveValue("fresh draft");
    expect(bootstrap.starts).toBe(1);
  });
});

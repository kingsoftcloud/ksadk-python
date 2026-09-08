import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { apiFetch } from "../api";
import { ChatWorkspace } from "./ChatWorkspace";

const mocks = vi.hoisted(() => {
  const chat = {
    bootstrapStatus: "ready",
    bootstrapErrorMessage: "",
    sessions: [
      {
        SessionId: "session-1",
        Title: "已有会话",
        UpdatedAt: "2026-09-04T00:00:00Z",
        ActiveRunStatus: "",
      },
    ],
    currentSessionId: "session-1",
    isLoadingSessions: false,
    hasMoreSessions: false,
    isMobile: false,
    isStreaming: false,
    uiCapabilities: {
      StopRun: true,
      Attachments: true,
      Approval: true,
      ApprovalPolicy: { Enabled: true },
      Thinking: true,
      RuntimeCapabilityMatrix: {},
    },
    pendingInteractions: [{ interaction_id: "interaction-1" }],
    interactionRecords: [],
    localCatalog: [],
    createNewSession: vi.fn(),
    deleteSession: vi.fn(),
    selectSession: vi.fn(),
    loadMoreSessions: vi.fn(),
    loadOlderMessages: vi.fn(),
    refresh: vi.fn(),
    send: vi.fn(),
    stop: vi.fn(),
    cancelRemote: vi.fn(),
    deleteResponseFeedback: vi.fn(),
    submitResponseFeedback: vi.fn(),
    respondToApproval: vi.fn(),
    submitAguiAction: vi.fn(),
    respondInteraction: vi.fn(),
  };
  return {
    chat,
    useAgentChat: vi.fn(() => chat),
    facadeOptions: [] as Array<Record<string, unknown>>,
    timelineProps: null as Record<string, unknown> | null,
    composerProps: null as Record<string, unknown> | null,
  };
});

vi.mock("@kingsoftcloud/ksadk-web/hooks", () => ({
  useAgentChat: mocks.useAgentChat,
}));
vi.mock("@kingsoftcloud/ksadk-web/runtime", () => ({
  ApiFacadeImpl: class {
    constructor(options: Record<string, unknown>) {
      mocks.facadeOptions.push(options);
    }
  },
}));
vi.mock("@kingsoftcloud/ksadk-web/chat/timeline", () => ({
  AgentConversationTimeline: (props: Record<string, unknown>) => {
    mocks.timelineProps = props;
    return <div data-testid="shared-timeline" />;
  },
}));
vi.mock("@kingsoftcloud/ksadk-web/chat/composer", () => ({
  AgentConversationComposer: (props: Record<string, unknown>) => {
    mocks.composerProps = props;
    return <div data-testid="shared-composer" />;
  },
}));
vi.mock("../api", () => ({ apiFetch: vi.fn() }));
vi.mock("./AgentAvatar", () => ({ AgentAvatar: () => <span data-testid="agent-avatar" /> }));
vi.mock("./ConfirmDialog", () => ({ ConfirmDialog: () => <div data-testid="confirm-dialog" /> }));

describe("ChatWorkspace shared conversation composition", () => {
  beforeEach(() => {
    mocks.chat.bootstrapStatus = "ready";
    mocks.chat.isStreaming = false;
    mocks.chat.currentSessionId = "session-1";
    mocks.useAgentChat.mockClear();
    mocks.facadeOptions.length = 0;
    mocks.timelineProps = null;
    mocks.composerProps = null;
    Object.values(mocks.chat).forEach(value => {
      if (typeof value === "function" && "mockClear" in value) value.mockClear();
    });
  });

  it("reconciles the same session when the transport ends without duplicating a run", () => {
    const { rerender } = render(<ChatWorkspace agentId="local-1" agentName="Agent" />);
    expect(mocks.chat.refresh).not.toHaveBeenCalled();
    mocks.chat.isStreaming = true;
    rerender(<ChatWorkspace agentId="local-1" agentName="Agent" />);
    expect(mocks.chat.refresh).not.toHaveBeenCalled();
    mocks.chat.isStreaming = false;
    rerender(<ChatWorkspace agentId="local-1" agentName="Agent" />);
    expect(mocks.chat.refresh).toHaveBeenCalledOnce();
    expect(mocks.chat.send).not.toHaveBeenCalled();
    rerender(<ChatWorkspace agentId="local-1" agentName="Agent" />);
    expect(mocks.chat.refresh).toHaveBeenCalledOnce();
  });

  it("does not reconcile another session when switching away from a running session", () => {
    mocks.chat.isStreaming = true;
    const { rerender } = render(<ChatWorkspace agentId="local-1" agentName="Agent" />);
    mocks.chat.currentSessionId = "session-2";
    mocks.chat.isStreaming = false;
    rerender(<ChatWorkspace agentId="local-1" agentName="Agent" />);
    expect(mocks.chat.refresh).not.toHaveBeenCalled();
  });

  it("keeps bootstrap progress quiet and inside the transcript region", () => {
    mocks.chat.bootstrapStatus = "loading";
    render(<ChatWorkspace agentId="local-1" agentName="本地 Agent" />);

    expect(screen.getByRole("status", { name: "正在连接 Agent" })).toHaveClass("chat-bootstrap-loading");
    expect(screen.queryByText("正在连接 Agent…")).not.toBeInTheDocument();
  });

  it("binds the shared controller to the selected agent and Studio fetch", () => {
    render(<ChatWorkspace agentId="ar-cloud-1" agentName="云端 Agent" />);

    expect(mocks.facadeOptions).toEqual([{ fetch: apiFetch, agentId: "ar-cloud-1" }]);
    expect(mocks.useAgentChat).toHaveBeenCalledWith(expect.objectContaining({
      agentId: "ar-cloud-1",
      conversationClient: null,
    }));
    expect(screen.getByTestId("shared-timeline")).toBeInTheDocument();
    expect(screen.getByTestId("shared-composer")).toBeInTheDocument();
  });

  it("passes stream, approval, HITL, and capability controls to shared components", () => {
    render(<ChatWorkspace agentId="local-1" agentName="本地 Agent" />);

    expect(mocks.timelineProps).toMatchObject({
      isMobile: false,
      onRespondToApproval: mocks.chat.respondToApproval,
      onSubmitAguiAction: mocks.chat.submitAguiAction,
      onCancelRemote: mocks.chat.cancelRemote,
    });
    expect(mocks.composerProps).toMatchObject({
      isMobile: false,
      attachmentsEnabled: true,
      approvalEnabled: true,
      thinkingEnabled: true,
      pendingInteractions: mocks.chat.pendingInteractions,
    });
  });

  it("keeps Studio session navigation and refresh wired to the shared controller", async () => {
    const { rerender } = render(
      <ChatWorkspace agentId="local-1" agentName="本地 Agent" refreshTick={0} />,
    );

    fireEvent.click(screen.getByRole("button", { name: "新对话" }));
    fireEvent.click(screen.getByRole("button", { name: "已有会话" }));
    expect(mocks.chat.createNewSession).toHaveBeenCalledOnce();
    expect(mocks.chat.selectSession).toHaveBeenCalledWith("session-1");

    rerender(<ChatWorkspace agentId="local-1" agentName="本地 Agent" refreshTick={1} />);
    await waitFor(() => expect(mocks.chat.refresh).toHaveBeenCalledOnce());
  });
});

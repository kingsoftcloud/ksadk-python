import { createRef } from "react";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { apiFetch } from "../api";
import { ChatWorkspace, type ChatWorkspaceHandle } from "./ChatWorkspace";

const mocks = vi.hoisted(() => {
  const chat = {
    agentId: "local-1",
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
    startNewConversation: vi.fn(),
    deleteSession: vi.fn(),
    selectSession: vi.fn(),
    loadMoreSessions: vi.fn(),
    loadOlderMessages: vi.fn(),
    searchConversation: vi.fn(),
    messageHistory: { hasMore: false },
    refresh: vi.fn(),
    send: vi.fn(),
    stop: vi.fn(),
    cancelRemote: vi.fn(),
    deleteResponseFeedback: vi.fn(),
    submitResponseFeedback: vi.fn(),
    respondToApproval: vi.fn(),
    submitAguiAction: vi.fn(),
    respondInteraction: vi.fn(),
    conversationDrafts: undefined as any,
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
    mocks.chat.isLoadingSessions = false;
    mocks.chat.isStreaming = false;
    mocks.chat.currentSessionId = "session-1";
    mocks.chat.sessions = [
      {
        SessionId: "session-1",
        Title: "已有会话",
        UpdatedAt: "2026-09-04T00:00:00Z",
        ActiveRunStatus: "",
      },
    ];
    mocks.useAgentChat.mockClear();
    mocks.facadeOptions.length = 0;
    mocks.timelineProps = null;
    mocks.composerProps = null;
    mocks.chat.conversationDrafts = undefined;
    Object.values(mocks.chat).forEach(value => {
      if (typeof value === "function" && "mockClear" in value) value.mockClear();
    });
  });

  it("shows a product title for an empty session instead of its internal id", () => {
    mocks.chat.sessions = [{
      SessionId: "ses_internal_id",
      Title: "ses_internal_id",
      UpdatedAt: "2026-09-04T00:00:00Z",
      ActiveRunStatus: "",
    }];
    mocks.chat.currentSessionId = "ses_internal_id";

    render(<ChatWorkspace agentId="local-1" agentName="Agent" />);

    expect(screen.getByText("新会话")).toBeInTheDocument();
    expect(screen.queryByText("ses_internal_id")).not.toBeInTheDocument();
  });

  it("scopes conversation storage by the opaque credential tenant scope", () => {
    render(
      <ChatWorkspace
        agentId="local-1"
        agentName="Agent"
        workspaceId="workspace-a"
        credentialScope="tenant-a"
        targetId="target-a"
      />,
    );

    const lastCall = mocks.useAgentChat.mock.calls[mocks.useAgentChat.mock.calls.length - 1] as unknown[] | undefined;
    const options = lastCall?.[0] as { conversationController: { storageKey?: string } };
    expect(options.conversationController.storageKey).toBe("ksadk.studio:workspace-a:tenant-a:local-1:target-a");
  });

  it("surfaces a multi-window draft conflict and resolves the selected side", () => {
    let notify!: () => void;
    let conflicted = true;
    const conflictStore = {
      getConflict: vi.fn(() => conflicted ? { conversationId: "conversation_conflict", local: { text: "本窗口", revision: 1, updatedAt: 1, attachments: [] }, remote: { text: "另一个窗口", revision: 1, updatedAt: 2 }, detectedAt: 2 } : undefined),
      subscribe: vi.fn((listener: () => void) => { notify = listener; return () => {}; }),
      resolveConflict: vi.fn(() => { conflicted = false; notify(); }),
    };
    mocks.chat.conversationDrafts = conflictStore;
    render(<ChatWorkspace agentId="local-1" agentName="Agent" />);

    expect(screen.getByRole("alert")).toHaveTextContent("检测到其他窗口修改了草稿");
    fireEvent.click(screen.getByRole("button", { name: "使用其他窗口" }));
    expect(conflictStore.resolveConflict).toHaveBeenCalledWith(expect.any(String), "remote");
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    delete mocks.chat.conversationDrafts;
  });

  it("finds the active conversation body and leaves browser find available on other pages", () => {
    const props = { agentId: "local-1", agentName: "Agent" };
    const { rerender } = render(<ChatWorkspace {...props} />);
    const shortcut = new KeyboardEvent("keydown", { key: "f", metaKey: true, cancelable: true });
    fireEvent(window, shortcut);
    expect(shortcut.defaultPrevented).toBe(true);
    expect(screen.getByRole("searchbox", { name: "查找当前会话正文" })).toHaveFocus();
    expect(screen.getByRole("searchbox", { name: "搜索会话" })).toHaveValue("");
    rerender(<ChatWorkspace {...props} active={false} />);
    const otherPage = new KeyboardEvent("keydown", { key: "f", ctrlKey: true, cancelable: true });
    fireEvent(window, otherPage);
    expect(otherPage.defaultPrevented).toBe(false);
    expect(screen.queryByRole("region", { name: "查找当前会话" })).not.toBeInTheDocument();
  });

  it("passes the chosen history result to the active timeline", async () => {
    mocks.chat.searchConversation.mockResolvedValue({ matches: [{ messageId: "old-user", role: "user", excerpt: "历史目标" }],
      matchedMessages: 1, searchedMessages: 2000, complete: true });
    render(<ChatWorkspace agentId="local-1" agentName="Agent" />);
    fireEvent.click(screen.getByRole("button", { name: "查找当前会话" }));
    fireEvent.change(screen.getByRole("searchbox", { name: "查找当前会话正文" }), { target: { value: "历史目标" } });
    fireEvent.click(await screen.findByRole("button", { name: /历史目标/ }));
    expect(mocks.timelineProps?.revealMessage).toEqual({ id: "old-user", request: 1 });
    fireEvent.click(screen.getByRole("button", { name: /历史目标/ }));
    expect(mocks.timelineProps?.revealMessage).toEqual({ id: "old-user", request: 2 });
  });

  it("reports the selected session so the host inspector follows it", async () => {
    const onSessionChanged = vi.fn();
    render(
      <ChatWorkspace
        agentId="local-1"
        agentName="Agent"
        onSessionChanged={onSessionChanged}
      />,
    );

    await waitFor(() => expect(onSessionChanged).toHaveBeenCalledWith("session-1"));
  });

  it("opens a scheduled result only after bootstrap and session loading settle", async () => {
    mocks.chat.bootstrapStatus = "loading";
    const view = <ChatWorkspace agentId="local-1" agentName="Agent" requestedSessionId="scheduled-session" />;
    const { rerender } = render(view);
    expect(mocks.chat.selectSession).not.toHaveBeenCalled();
    mocks.chat.bootstrapStatus = "ready";
    mocks.chat.isLoadingSessions = true;
    rerender(<ChatWorkspace agentId="local-1" agentName="Agent" requestedSessionId="scheduled-session" />);
    expect(mocks.chat.selectSession).not.toHaveBeenCalled();
    mocks.chat.isLoadingSessions = false;
    rerender(<ChatWorkspace agentId="local-1" agentName="Agent" requestedSessionId="scheduled-session" />);
    await waitFor(() => expect(mocks.chat.selectSession).toHaveBeenCalledExactlyOnceWith("scheduled-session"));
    rerender(<ChatWorkspace agentId="local-1" agentName="Agent" requestedSessionId="scheduled-session" refreshTick={1} />);
    expect(mocks.chat.selectSession).toHaveBeenCalledTimes(1);
  });

  it("keeps one history surface and preserves its filter across product navigation", () => {
    const historyHost = document.createElement("div");
    const headerHost = document.createElement("div");
    document.body.append(historyHost, headerHost);
    const onSelectConversation = vi.fn();
    const { container, rerender, unmount } = render(<ChatWorkspace agentId="local-1" agentName="Agent"
      integratedHistory historyHost={historyHost} headerHost={headerHost} onSelectConversation={onSelectConversation}/>);
    expect(within(container).queryByRole("complementary", { name: "会话历史" })).toBeNull();
    expect(within(historyHost).getByRole("complementary", { name: "会话历史" })).toBeInTheDocument();
    fireEvent.change(within(historyHost).getByRole("searchbox"), { target: { value: "已有" } });
    rerender(<ChatWorkspace agentId="local-1" agentName="Agent" active={false}
      integratedHistory historyHost={historyHost} headerHost={headerHost} onSelectConversation={onSelectConversation}/>);
    expect(headerHost).toBeEmptyDOMElement();
    expect(within(historyHost).getByRole("searchbox")).toHaveValue("已有");
    fireEvent.click(within(historyHost).getByRole("button", { name: "已有会话" }));
    expect(mocks.chat.selectSession).toHaveBeenCalledWith("session-1");
    expect(onSelectConversation).toHaveBeenCalledOnce();
    unmount(); historyHost.remove(); headerHost.remove();
  });

  it("creates a local draft immediately without waiting for bootstrap or session loading", async () => {
    mocks.chat.bootstrapStatus = "loading";
    const onNewChatStarted = vi.fn();
    const props = { agentId: "local-1", agentName: "Agent", newChatRequest: 1, onNewChatStarted };
    const { rerender } = render(<ChatWorkspace {...props}/>);
    expect(mocks.chat.startNewConversation).toHaveBeenCalledOnce();
    mocks.chat.bootstrapStatus = "ready";
    mocks.chat.isLoadingSessions = true;
    rerender(<ChatWorkspace {...props}/>);
    expect(mocks.chat.startNewConversation).toHaveBeenCalledOnce();
    mocks.chat.isLoadingSessions = false;
    rerender(<ChatWorkspace {...props}/>);
    expect(mocks.chat.startNewConversation).toHaveBeenCalledOnce();
    await waitFor(() => expect(onNewChatStarted).toHaveBeenCalledOnce());
    rerender(<ChatWorkspace {...props} refreshTick={1}/>);
    expect(mocks.chat.startNewConversation).toHaveBeenCalledOnce();
  });

  it("starts new conversations through the product rail without interrupting a stream", () => {
    const ref = createRef<ChatWorkspaceHandle>();
    const { rerender } = render(<ChatWorkspace ref={ref} agentId="local-1" agentName="Agent"/>);
    ref.current?.startNewChat();
    expect(mocks.chat.startNewConversation).toHaveBeenCalledOnce();
    mocks.chat.isStreaming = true;
    rerender(<ChatWorkspace ref={ref} agentId="local-1" agentName="Agent"/>);
    ref.current?.startNewChat();
    expect(mocks.chat.startNewConversation).toHaveBeenCalledTimes(2);
    expect(mocks.chat.stop).not.toHaveBeenCalled();
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
    expect(mocks.chat.startNewConversation).toHaveBeenCalledOnce();
    expect(mocks.chat.selectSession).toHaveBeenCalledWith("session-1");

    rerender(<ChatWorkspace agentId="local-1" agentName="本地 Agent" refreshTick={1} />);
    await waitFor(() => expect(mocks.chat.refresh).toHaveBeenCalledOnce());
  });
});

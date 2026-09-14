import { useCallback, useEffect, useRef, useState } from "react";
import { ArrowLeft, Plus, Search, UsersRound } from "lucide-react";
import {
  HttpTeamsClient,
  TEAMS_API_VERSION,
  type ExecutionBinding,
  type GroupMessageInput,
  type GroupReceipt,
  type GroupSummary,
  type TeamArtifact,
} from "@kingsoftcloud/ksadk-web/teams";
import {
  CreateGroupDialog,
  TeamWorkspace,
  useTeamChat,
  type TeamMemberCandidate,
} from "@kingsoftcloud/ksadk-web/teams/components";
import { apiFetch } from "../api";
import { TeamsGroupSettings } from "./TeamsGroupSettings";
import { TeamsMemberObserver } from "./TeamsMemberObserver";
import {
  readTeamsLifecycle,
  teamRequest,
  TeamsAvailability,
  type TeamsLifecycle,
} from "./TeamsAvailability";

const client = new HttpTeamsClient({ fetch: apiFetch });
const newKey = () => `studio-${crypto.randomUUID()}`;
const messageOf = (cause: unknown) =>
  cause instanceof Error ? cause.message : "请求失败，请重试。";
function requireReceipt(receipt: GroupReceipt) {
  if (receipt.status === "rejected" || receipt.status === "uncertain")
    throw new Error(receipt.reason || "执行端尚未确认，请核对后重试。");
  return receipt;
}
function routeSelection() {
  return new URLSearchParams(window.location.hash.split("?")[1] || "");
}
function writeSelection(
  groupId: string,
  selection: { teamRunId?: string; taskId?: string } = {},
) {
  const params = new URLSearchParams({ groupId });
  if (selection.teamRunId) params.set("teamRunId", selection.teamRunId);
  if (selection.taskId) params.set("taskId", selection.taskId);
  window.history.replaceState(null, "", `#/workspace/teams?${params}`);
}

export function TeamsPage() {
  const [lifecycle, setLifecycle] = useState<TeamsLifecycle | null>(null);
  const [error, setError] = useState("");
  const [retry, setRetry] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    setError("");
    void readTeamsLifecycle(controller.signal)
      .then((state) => {
        if (state.apiVersion !== TEAMS_API_VERSION)
          throw new Error("团队插件版本与当前界面不兼容，请更新插件。");
        setLifecycle(state);
      })
      .catch((cause) => {
        if (!controller.signal.aborted) setError(messageOf(cause));
      });
    return () => controller.abort();
  }, [retry]);
  if (error)
    return (
      <div className="studio-plugin-empty">
        <h2>团队服务暂不可用</h2>
        <p role="alert">{error}</p>
        <button
          className="button secondary"
          onClick={() => setRetry((value) => value + 1)}
        >
          重试
        </button>
      </div>
    );
  if (!lifecycle)
    return (
      <p className="studio-plugin-loading" role="status">
        正在连接团队服务…
      </p>
    );
  if (!lifecycle.enabled) return <TeamsAvailability />;
  return (
    <TeamsBrowser
      key={lifecycle.authorityRef}
      authorityRef={lifecycle.authorityRef}
    />
  );
}

function TeamsBrowser({ authorityRef }: { authorityRef: string }) {
  const [groups, setGroups] = useState<GroupSummary[]>([]);
  const [nextCursor, setNextCursor] = useState<string | undefined>();
  const [selected, setSelected] = useState(
    () => routeSelection().get("groupId") || "",
  );
  const [query, setQuery] = useState("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [createOpen, setCreateOpen] = useState(false);
  const [candidates, setCandidates] = useState<TeamMemberCandidate[]>([]);
  const [bindingsLoading, setBindingsLoading] = useState(false);
  const drafts = useRef(new Map<string, string>());
  const listGeneration = useRef(0);
  const reload = useCallback(
    async (signal?: AbortSignal, cursor?: string) => {
      const generation = ++listGeneration.current;
      try {
        const result = await client.list({ signal, cursor, limit: 50 });
        if (signal?.aborted || generation !== listGeneration.current) return;
        const scoped = result.items.filter(
          (group) => group.authorityRef === authorityRef,
        );
        setGroups((previous) =>
          cursor
            ? [
                ...previous.filter(
                  (group) =>
                    !scoped.some((row) => row.groupId === group.groupId),
                ),
                ...scoped,
              ]
            : scoped,
        );
        setNextCursor(result.nextCursor || undefined);
        setError("");
      } catch (cause) {
        if (!signal?.aborted) setError(messageOf(cause));
      } finally {
        if (!signal?.aborted) setLoading(false);
      }
    },
    [authorityRef],
  );
  useEffect(() => {
    const controller = new AbortController();
    void reload(controller.signal);
    const timer = window.setInterval(() => {
      if (document.visibilityState === "visible")
        void reload(controller.signal);
    }, 15_000);
    const sync = () => setSelected(routeSelection().get("groupId") || "");
    window.addEventListener("hashchange", sync);
    window.addEventListener("popstate", sync);
    return () => {
      controller.abort();
      clearInterval(timer);
      window.removeEventListener("hashchange", sync);
      window.removeEventListener("popstate", sync);
    };
  }, [reload]);
  useEffect(() => {
    if (!createOpen) return;
    const controller = new AbortController();
    setBindingsLoading(true);
    void teamRequest<{
      items: (ExecutionBinding & { name?: string; displayName?: string })[];
    }>("/api/v1/groups/bindings", { signal: controller.signal })
      .then((result) => {
        setCandidates(
          result.items.map((binding) => ({
            memberId: `member-${crypto.randomUUID()}`,
            name: binding.name || binding.displayName || binding.agentId,
            description: `${binding.kind === "a2a" ? "云端" : "本机"}${binding.buildId ? ` · ${binding.buildId}` : ""}${binding.capabilities.leader ? " · 可担任 Leader" : ""}`,
            binding,
          })),
        );
      })
      .catch((cause) => {
        if (!controller.signal.aborted) setError(messageOf(cause));
      })
      .finally(() => {
        if (!controller.signal.aborted) setBindingsLoading(false);
      });
    return () => controller.abort();
  }, [createOpen]);
  const choose = (groupId: string) => {
    setSelected(groupId);
    writeSelection(groupId);
  };
  const visible = groups.filter((group) =>
    `${group.name} ${group.lastMessage || ""}`
      .toLowerCase()
      .includes(query.toLowerCase()),
  );
  return (
    <div className="studio-teams-browser" data-selected={Boolean(selected)}>
      <aside className="studio-team-directory" aria-label="团队列表">
        <header>
          <div>
            <span className="studio-teams-eyebrow">协作空间 <b className="studio-beta-badge">Beta</b></span>
            <h2>团队</h2>
          </div>
          <button
            className="icon-button tertiary"
            aria-label="创建团队"
            onClick={() => setCreateOpen(true)}
          >
            <Plus size={19} />
          </button>
        </header>
        <label className="studio-team-search">
          <Search size={15} />
          <input
            aria-label="搜索团队"
            placeholder="搜索团队"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
          />
        </label>
        {error && (
          <div className="studio-team-list-error" role="alert">
            <p>{error}</p>
            <button className="button tertiary" onClick={() => void reload()}>
              重试
            </button>
          </div>
        )}
        <div className="studio-team-list">
          {loading ? (
            <p role="status">正在读取团队…</p>
          ) : visible.length ? (
            visible.map((group) => (
              <button
                key={group.groupId}
                className="studio-team-list-item"
                aria-current={selected === group.groupId ? "page" : undefined}
                onClick={() => choose(group.groupId)}
              >
                <span className="studio-team-monogram" aria-hidden="true">
                  {Array.from(group.name)[0]}
                </span>
                <span className="studio-team-list-copy">
                  <strong>{group.name}</strong>
                  <span>
                    {group.lastMessage ||
                      `${group.memberCount} 位成员 · 准备好开始协作`}
                  </span>
                  {group.pendingCount > 0 && (
                    <small>{group.pendingCount} 项待处理</small>
                  )}
                </span>
                {group.unreadCount > 0 && (
                  <span
                    className="studio-team-unread"
                    aria-label={`${group.unreadCount} 条未读`}
                  >
                    {Math.min(group.unreadCount, 99)}
                  </span>
                )}
              </button>
            ))
          ) : (
            <div className="studio-team-list-empty">
              <p>{query ? "没有匹配的团队" : "还没有团队"}</p>
              {!query && <span>把合适的 Agent 召集到一起。</span>}
            </div>
          )}
          {nextCursor && (
            <button
              className="button tertiary"
              onClick={() => void reload(undefined, nextCursor)}
            >
              加载更多团队
            </button>
          )}
        </div>
        <footer>
          <a href="#/agents">管理 Agent</a>
          <a href="#/plugins">插件设置</a>
        </footer>
      </aside>
      <div className="studio-team-content">
        {selected ? (
          <>
            <button
              className="studio-team-directory-back"
              onClick={() => choose("")}
            >
              <ArrowLeft size={16} />
              团队列表
            </button>
            <StudioTeamGroup
              key={`${authorityRef}:${selected}`}
              authorityRef={authorityRef}
              groupId={selected}
              initialDraft={drafts.current.get(selected) || ""}
              onDraft={(draft) => drafts.current.set(selected, draft)}
              onChanged={() => void reload()}
            />
          </>
        ) : (
          <div className="studio-team-welcome">
            <span className="studio-teams-symbol">
              <UsersRound size={26} />
            </span>
            <span className="studio-teams-eyebrow">AGENT TEAMS <b className="studio-beta-badge">Beta</b></span>
            <h2>一个目标，团队一起完成</h2>
            <p>
              让 Leader 组织分工，让成员专注任务。
              <br />
              在群聊中跟进过程、处理审批和验收成果。
            </p>
            <button
              className="button primary"
              onClick={() => setCreateOpen(true)}
            >
              <Plus size={16} />
              创建团队
            </button>
            <a href="#/agents">查看可用 Agent</a>
          </div>
        )}
      </div>
      <CreateGroupDialog
        open={createOpen}
        loading={bindingsLoading}
        candidates={candidates}
        onClose={() => setCreateOpen(false)}
        onCreate={async (input) => {
          const snapshot = await client.create(input);
          choose(snapshot.group.groupId);
          await reload();
        }}
      />
    </div>
  );
}

function StudioTeamGroup({
  groupId,
  authorityRef,
  initialDraft,
  onDraft,
  onChanged,
}: {
  groupId: string;
  authorityRef: string;
  initialDraft: string;
  onDraft: (draft: string) => void;
  onChanged: () => void;
}) {
  const chat = useTeamChat({ client, groupId, authorityRef });
  const [draft, setDraft] = useState(initialDraft);
  const [manage, setManage] = useState(false);
  const [artifactError, setArtifactError] = useState("");
  const initialSelection = useRef({
    teamRunId: routeSelection().get("teamRunId") || "",
    taskId: routeSelection().get("taskId") || "",
  });
  const pending = useRef(new Map<string, string>());
  const actionKey = (identity: unknown[]) => {
    const digest = JSON.stringify(identity);
    if (!pending.current.has(digest)) pending.current.set(digest, newKey());
    return pending.current.get(digest)!;
  };
  useEffect(() => {
    if (!chat.snapshot) return;
    const controller = new AbortController();
    const timer = setTimeout(() => {
      if (document.visibilityState === "visible")
        void client
          .markRead(groupId, chat.snapshot!.watermark, controller.signal)
          .catch(() => {});
    }, 500);
    return () => {
      clearTimeout(timer);
      controller.abort();
    };
  }, [groupId, chat.snapshot?.watermark]);
  function changeDraft(value: string) {
    setDraft(value);
    onDraft(value);
  }
  async function send(input: GroupMessageInput) {
    const receipt = requireReceipt(await client.send(groupId, input));
    onChanged();
    return receipt;
  }
  return (
    <>
      <TeamWorkspace
        snapshot={chat.snapshot}
        connection={chat.connection}
        loading={chat.loading}
        error={artifactError || chat.error}
        draft={draft}
        onDraftChange={changeDraft}
        onSend={send}
        onRetry={chat.reconnect}
        initialSelection={initialSelection.current}
        onSelectionChange={(selection) => writeSelection(groupId, selection)}
        onManage={() => setManage(true)}
        onControl={async (run, action) =>
          requireReceipt(
            await client.control(groupId, run.teamRunId, {
              action,
              expectedRevision: run.revision,
              idempotencyKey: actionKey([
                "control",
                run.teamRunId,
                run.revision,
                action,
              ]),
            }),
          )
        }
        onAcceptRun={async (run, accepted) =>
          requireReceipt(
            await client.acceptRun(groupId, run.teamRunId, {
              accepted,
              expectedRevision: run.revision,
              idempotencyKey: actionKey([
                "accept",
                run.teamRunId,
                run.revision,
                accepted,
              ]),
            }),
          )
        }
        onTaskAction={async (task, action) =>
          requireReceipt(
            await client.taskAction(groupId, task.taskId, {
              action,
              expectedRevision: task.revision,
              idempotencyKey: actionKey([
                "task",
                task.taskId,
                task.revision,
                action,
              ]),
            }),
          )
        }
        onRespondInteraction={async (input) =>
          requireReceipt(await client.interaction(groupId, input))
        }
        onOpenArtifact={(artifact: TeamArtifact) => {
          const expected = `/api/v1/groups/${encodeURIComponent(groupId)}/artifacts/${encodeURIComponent(artifact.artifactId)}/download`;
          if (
            artifact.source.groupId !== groupId ||
            artifact.source.authorityRef !== authorityRef ||
            artifact.uri !== expected
          ) {
            setArtifactError("交付物尚无有效的团队下载链接。");
            return;
          }
          setArtifactError("");
          const link = document.createElement("a");
          link.href = expected;
          link.download = artifact.name;
          link.rel = "noopener";
          link.click();
        }}
        onLoadExecution={(teamRunId, signal) =>
          client.execution(groupId, teamRunId, signal)
        }
        renderMember={(member, actions, source) => (
          <TeamsMemberObserver
            key={member.memberId}
            member={member}
            snapshot={chat.snapshot!}
            requestedSource={source}
            onDirectedMessage={actions.directedMessage}
          />
        )}
      />
      {manage && chat.snapshot && (
        <TeamsGroupSettings
          client={client}
          snapshot={chat.snapshot}
          onClose={() => setManage(false)}
          onSaved={() => {
            chat.reconnect();
            onChanged();
          }}
        />
      )}
    </>
  );
}

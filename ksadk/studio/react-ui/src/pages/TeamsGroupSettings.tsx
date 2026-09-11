import { useEffect, useRef, useState } from "react";
import {
  type HttpTeamsClient,
  type GroupSnapshot,
  type ExecutionBinding,
} from "@kingsoftcloud/ksadk-web/teams";
import { teamRequest } from "./TeamsAvailability";
const newKey = () => `studio-${crypto.randomUUID()}`;
const messageOf = (cause: unknown) =>
  cause instanceof Error ? cause.message : "请求失败，请重试。";

export function TeamsGroupSettings({
  client,
  snapshot,
  onClose,
  onSaved,
}: {
  client: HttpTeamsClient;
  snapshot: GroupSnapshot;
  onClose: () => void;
  onSaved: () => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const baseline = useRef(snapshot).current;
  const [name, setName] = useState(snapshot.group.name);
  const [leader, setLeader] = useState(snapshot.group.leaderMemberId);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [removeMemberId, setRemoveMemberId] = useState("");
  const [memberAction, setMemberAction] = useState<"none" | "add" | "rebind">(
    "none",
  );
  const [rebindMemberId, setRebindMemberId] = useState("");
  const [bindingRef, setBindingRef] = useState("");
  const [memberName, setMemberName] = useState("");
  const [bindings, setBindings] = useState<
    (ExecutionBinding & { name?: string })[]
  >([]);
  const newMemberId = useRef(`member-${crypto.randomUUID()}`).current;
  const [taskAcceptance, setTaskAcceptance] = useState(
    snapshot.group.policy?.taskAcceptance || "human",
  );
  const [peerWake, setPeerWake] = useState(
    snapshot.group.policy?.peerWake || false,
  );
  const active = snapshot.teamRuns.some(
    (run) => !["succeeded", "failed", "cancelled"].includes(run.status),
  );
  const mutation = useRef<{ digest: string; key: string } | null>(null);
  useEffect(() => {
    const controller = new AbortController();
    void teamRequest<{ items: (ExecutionBinding & { name?: string })[] }>(
      "/api/v1/groups/bindings",
      { signal: controller.signal },
    )
      .then((result) => setBindings(result.items))
      .catch((cause) => {
        if (!controller.signal.aborted) setError(messageOf(cause));
      });
    return () => controller.abort();
  }, []);
  useEffect(() => {
    const previous = document.activeElement as HTMLElement;
    dialog.current?.showModal();
    return () => {
      dialog.current?.close();
      previous?.focus();
    };
  }, []);
  async function save() {
    if (!name.trim() || busy) return;
    setBusy(true);
    setError("");
    if (
      memberAction !== "none" &&
      (!bindingRef ||
        !memberName.trim() ||
        (memberAction === "rebind" && !rebindMemberId))
    ) {
      setError("请选择成员和可用 Agent，并填写成员名称。");
      setBusy(false);
      return;
    }
    const memberChange = {
      memberId: memberAction === "rebind" ? rebindMemberId : newMemberId,
      name: memberName.trim(),
      bindingRef,
    };
    const patch = {
      ...(memberAction === "add"
        ? { addMember: memberChange }
        : memberAction === "rebind"
          ? { rebindMember: memberChange }
          : {}),
      ...(name.trim() !== baseline.group.name ? { name: name.trim() } : {}),
      ...(leader !== baseline.group.leaderMemberId
        ? { leaderMemberId: leader }
        : {}),
      ...(removeMemberId ? { removeMemberId } : {}),
      ...(taskAcceptance !== (baseline.group.policy?.taskAcceptance || "human")
        ? { taskAcceptance }
        : {}),
      ...(peerWake !== (baseline.group.policy?.peerWake || false)
        ? { peerWake }
        : {}),
    };
    const digest = JSON.stringify([patch, baseline.group.revision]);
    if (mutation.current?.digest !== digest)
      mutation.current = { digest, key: newKey() };
    try {
      await client.update(snapshot.group.groupId, {
        ...patch,
        expectedRevision: baseline.group.revision,
        idempotencyKey: mutation.current.key,
      });
      onSaved();
      onClose();
    } catch (cause) {
      setError(messageOf(cause));
    } finally {
      setBusy(false);
    }
  }
  return (
    <dialog
      ref={dialog}
      className="ksadk-teams team-create-dialog"
      aria-label="团队设置"
      onCancel={(event) => {
        event.preventDefault();
        if (!busy) onClose();
      }}
    >
      <form
        onSubmit={(event) => {
          event.preventDefault();
          void save();
        }}
      >
        <header>
          <h2>团队设置</h2>
          <button
            type="button"
            className="team-button team-icon-button"
            aria-label="关闭团队设置"
            disabled={busy}
            onClick={onClose}
          >
            ×
          </button>
        </header>
        <label className="team-field">
          群组名称
          <input
            value={name}
            maxLength={100}
            required
            onChange={(event) => setName(event.target.value)}
            disabled={busy}
          />
        </label>
        <label className="team-field">
          Leader
          <select
            value={leader}
            onChange={(event) => setLeader(event.target.value)}
            disabled={busy || active}
          >
            {snapshot.members
              .filter(
                (member) =>
                  member.status === "active" &&
                  member.binding.capabilities.leader,
              )
              .map((member) => (
                <option key={member.memberId} value={member.memberId}>
                  {member.name}
                </option>
              ))}
          </select>
        </label>
        <label className="team-field">
          任务验收
          <select
            value={taskAcceptance}
            onChange={(event) =>
              setTaskAcceptance(event.target.value as "human" | "result")
            }
            disabled={busy || active}
          >
            <option value="human">由我逐项验收</option>
            <option value="result">以执行结果验收任务</option>
          </select>
          <small>本轮最终成果始终由你验收。</small>
        </label>
        <label className="team-field team-policy-checkbox">
          <span>
            <input
              type="checkbox"
              checked={peerWake}
              onChange={(event) => setPeerWake(event.target.checked)}
              disabled={busy || active}
            />
            允许成员互相唤醒
          </span>
          <small>成员之间的协作仍受本轮预算和停止操作约束。</small>
        </label>
        <fieldset
          className="studio-team-member-settings"
          disabled={busy || active}
        >
          <legend>添加成员或更换 Agent</legend>
          <label className="team-field">
            成员变更
            <select
              value={memberAction}
              onChange={(event) => {
                setMemberAction(
                  event.target.value as "none" | "add" | "rebind",
                );
                setBindingRef("");
                setMemberName("");
              }}
            >
              <option value="none">保留当前绑定</option>
              <option
                value="add"
                disabled={
                  snapshot.members.filter(
                    (member) => member.status !== "removed",
                  ).length >= 8
                }
              >
                添加一位成员
              </option>
              <option value="rebind">为成员更换 Agent</option>
            </select>
          </label>
          {memberAction !== "none" && (
            <>
              {memberAction === "rebind" && (
                <label className="team-field">
                  选择要更换的成员
                  <select
                    value={rebindMemberId}
                    onChange={(event) => {
                      setRebindMemberId(event.target.value);
                      setBindingRef("");
                    }}
                  >
                    <option value="">请选择成员</option>
                    {snapshot.members
                      .filter((member) => member.status !== "removed")
                      .map((member) => (
                        <option key={member.memberId} value={member.memberId}>
                          {member.name}
                        </option>
                      ))}
                  </select>
                </label>
              )}
              <label className="team-field">
                可用 Agent
                <select
                  value={bindingRef}
                  onChange={(event) => {
                    setBindingRef(event.target.value);
                    const binding = bindings.find(
                      (row) => row.bindingRef === event.target.value,
                    );
                    setMemberName(binding?.name || binding?.agentId || "");
                  }}
                >
                  <option value="">选择已构建或已部署的 Agent</option>
                  {bindings
                    .filter(
                      (binding) =>
                        binding.capabilities.enqueue &&
                        (memberAction !== "rebind" ||
                          rebindMemberId !== leader ||
                          binding.capabilities.leader),
                    )
                    .map((binding) => (
                      <option
                        key={binding.bindingRef}
                        value={binding.bindingRef}
                      >
                        {binding.name || binding.agentId}
                        {binding.buildId ? ` · ${binding.buildId}` : ""}
                      </option>
                    ))}
                </select>
              </label>
              <label className="team-field">
                成员名称
                <input
                  value={memberName}
                  maxLength={80}
                  onChange={(event) => setMemberName(event.target.value)}
                />
              </label>
              <p className="team-muted">
                更换 Agent 会使用新的独立会话，历史执行记录仍可查看。
              </p>
            </>
          )}
        </fieldset>
        <label className="team-field">
          移除成员
          <select
            value={removeMemberId}
            onChange={(event) => setRemoveMemberId(event.target.value)}
            disabled={busy || active}
          >
            <option value="">保留当前成员</option>
            {snapshot.members
              .filter(
                (member) =>
                  member.status === "active" && member.memberId !== leader,
              )
              .map((member) => (
                <option key={member.memberId} value={member.memberId}>
                  {member.name}
                </option>
              ))}
          </select>
        </label>
        <p className="team-muted">
          {active
            ? "本轮正在进行，结束或停止后可修改成员和协作策略。群名可以随时修改。"
            : "配置变化不会重置已有任务记录。"}
        </p>
        {error && (
          <p className="team-inline-error" role="alert">
            {error}
          </p>
        )}
        <footer>
          <button
            className="team-button"
            type="button"
            disabled={busy}
            onClick={onClose}
          >
            取消
          </button>
          <button className="team-button team-primary" disabled={busy}>
            {busy ? "正在保存" : "保存设置"}
          </button>
        </footer>
      </form>
    </dialog>
  );
}

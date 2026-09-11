import { useEffect, useMemo, useRef, useState, useSyncExternalStore } from "react";
import {
  createChatScope,
  HttpTeamsClient,
  memberStreamKey,
  createHttpMemberTransport,
  type AgentMember,
  type GroupSnapshot,
  type MemberStreamRef,
} from "@kingsoftcloud/ksadk-web/teams";
import { MemberInspector } from "@kingsoftcloud/ksadk-web/teams/components";
import { apiFetch } from "../api";
const teamsClient = new HttpTeamsClient({ fetch: apiFetch });
const memberTransport = createHttpMemberTransport({ fetch: apiFetch });

export function TeamsMemberObserver({
  member,
  snapshot,
  onDirectedMessage,
  requestedSource,
}: {
  member: AgentMember;
  snapshot: GroupSnapshot;
  requestedSource?: MemberStreamRef;
  onDirectedMessage: () => void;
}) {
  const requestedKey = requestedSource ? memberStreamKey(requestedSource) : "";
  const [selectedRun, setSelectedRun] = useState(requestedKey);
  useEffect(() => { if (requestedKey) setSelectedRun(requestedKey); }, [requestedKey]);
  const sources = [
    ...snapshot.tasks.flatMap((task) =>
      task.attempts.map((attempt) => attempt.source),
    ),
    ...snapshot.messages.flatMap((message) => message.sourceRefs || []),
    ...snapshot.interactions.map((interaction) => interaction.ref),
  ].filter((ref): ref is MemberStreamRef =>
    Boolean(ref && ref.memberId === member.memberId),
  );
  if (member.activeRunId)
    sources.push({
      authorityRef: snapshot.group.authorityRef,
      groupId: snapshot.group.groupId,
      memberId: member.memberId,
      bindingRef: member.bindingRef,
      providerRef: member.binding.providerRef,
      sessionId: member.sessionId,
      runId: member.activeRunId,
    });
  if (requestedSource) sources.push(requestedSource);
  const references = [
    ...new Map(sources.map((ref) => [memberStreamKey(ref), ref])).values(),
  ];
  const ref =
    references.find((ref) => memberStreamKey(ref) === selectedRun) || references.at(-1);
  if (!ref)
    return (
      <MemberInspector member={member} onDirectedMessage={onDirectedMessage} />
    );
  return (
    <>
      {references.length > 1 && (
        <label className="team-field">
          成员执行记录
          <select
            value={memberStreamKey(ref)}
            onChange={(event) => setSelectedRun(event.target.value)}
          >
            {references.map((row, index) => (
              <option key={memberStreamKey(row)} value={memberStreamKey(row)}>
                {row.runId === member.activeRunId
                  ? "当前执行"
                  : `历史执行 ${index + 1}`}{" "}
                · {row.runId.slice(-10)}
              </option>
            ))}
          </select>
        </label>
      )}
      <MemberObservation
        key={memberStreamKey(ref)}
        member={member}
        streamRef={ref}
        onDirectedMessage={onDirectedMessage}
      />
    </>
  );
}
function MemberObservation({
  member,
  streamRef,
  onDirectedMessage,
}: {
  member: AgentMember;
  streamRef: MemberStreamRef;
  onDirectedMessage: () => void;
}) {
  const scope = useMemo(
    () => createChatScope(streamRef),
    [
      streamRef.authorityRef,
      streamRef.groupId,
      streamRef.memberId,
      streamRef.bindingRef,
      streamRef.providerRef,
      streamRef.sessionId,
      streamRef.runId,
    ],
  );
  const observation = useSyncExternalStore(
    scope.subscribe,
    scope.getSnapshot,
    scope.getSnapshot,
  );
  useEffect(() => {
    void scope.observe(memberTransport);
    return () => scope.disconnect();
  }, [scope]);
  const [cancelBusy, setCancelBusy] = useState(false);
  const [cancelPending, setCancelPending] = useState(false);
  const [cancelError, setCancelError] = useState<string | null>(null);
  const cancelLock = useRef(false);
  const cancelKey = useRef(crypto.randomUUID());
  const canCancel = member.status === "active" && member.binding.capabilities.cancel &&
    streamRef.runId === member.activeRunId && streamRef.bindingRef === member.bindingRef &&
    streamRef.providerRef === member.binding.providerRef && streamRef.sessionId === member.sessionId;
  async function cancel() {
    if (!canCancel || cancelLock.current || cancelPending) return;
    cancelLock.current = true;
    setCancelBusy(true);
    setCancelError(null);
    try {
      const result = await teamsClient.cancelMember(streamRef.groupId, member.memberId, {
        ref: streamRef,
        idempotencyKey: cancelKey.current,
      });
      if (result.status !== "cancel_requested" || result.runId !== streamRef.runId) throw new Error("停止回执与当前执行不一致，请重新读取群状态。");
      setCancelPending(true);
    } catch (error) {
      setCancelError(error instanceof Error ? error.message : "停止请求提交失败，请重试。");
    } finally {
      cancelLock.current = false;
      setCancelBusy(false);
    }
  }
  return (
    <>
    <MemberInspector
      member={member}
      observation={observation}
      onCancel={canCancel ? () => void cancel() : undefined}
      cancelBusy={cancelBusy}
      cancelPending={cancelPending}
      onDirectedMessage={onDirectedMessage}
      onRetry={() => void scope.observe(memberTransport)}
    />
    {canCancel && cancelPending && <p role="status" className="team-observer-note">已请求停止，等待执行端确认。后续排队消息仍会保留。</p>}
    {cancelError && <p role="alert" className="team-inline-error">{cancelError}</p>}
    </>
  );
}

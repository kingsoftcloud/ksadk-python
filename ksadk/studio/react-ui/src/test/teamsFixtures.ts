import {
  TEAMS_API_VERSION,
  type GroupEvent,
  type GroupSnapshot,
  type MemberStreamRef,
} from "@kingsoftcloud/ksadk-web/teams";

/** Deliberately synthetic identities; used only by tests and their labelled fixture. */
export function teamSnapshot(): GroupSnapshot {
  const capabilities = {
    enqueue: true,
    cancel: true,
    steer: false,
    restore: true,
    interaction: true,
    leader: true,
  };
  return {
    apiVersion: TEAMS_API_VERSION,
    group: {
      groupId: "fixture-group",
      name: "接口改造协作组",
      authorityRef: "fixture-local",
      tenantId: "fixture-tenant",
      ownerSubject: "fixture-owner",
      leaderMemberId: "leader",
      revision: 1,
      status: "active",
      createdAt: "2026-09-10T08:00:00Z",
      updatedAt: "2026-09-10T08:00:00Z",
    },
    watermark: 0,
    members: [
      {
        memberId: "leader",
        groupId: "fixture-group",
        name: "协调助手",
        role: "leader",
        bindingRef: "binding-leader",
        binding: {
          bindingRef: "binding-leader",
          providerRef: "fixture-provider",
          kind: "local_build",
          agentId: "agent-leader",
          capabilities,
        },
        sessionId: "session-leader",
        revision: 1,
        status: "active",
        executionStatus: "idle",
      },
      {
        memberId: "engineer",
        groupId: "fixture-group",
        name: "工程师",
        role: "member",
        bindingRef: "binding-engineer",
        binding: {
          bindingRef: "binding-engineer",
          providerRef: "fixture-provider",
          kind: "local_build",
          agentId: "agent-engineer",
          capabilities: { ...capabilities, leader: false },
        },
        sessionId: "session-engineer",
        revision: 1,
        status: "active",
        executionStatus: "running",
        activeRunId: "run-engineer",
      },
    ],
    messages: [
      {
        messageId: "message-goal",
        groupId: "fixture-group",
        revision: 1,
        createdAt: "2026-09-10T08:00:00Z",
        senderPrincipal: "fixture-owner",
        senderName: "我",
        groupRole: "owner",
        parts: [
          {
            kind: "text",
            text: "分析接口改造范围，输出实现方案和兼容性验证结果。",
          },
        ],
        mentions: [],
        intent: "start_goal",
        visibility: "public",
      },
      {
        messageId: "message-leader",
        groupId: "fixture-group",
        revision: 1,
        createdAt: "2026-09-10T08:01:00Z",
        senderPrincipal: "fixture-leader",
        senderName: "协调助手",
        groupRole: "leader",
        memberId: "leader",
        parts: [
          {
            kind: "text",
            text: "我会先梳理接口契约，由工程师验证实现边界。\n\n两项结果验收后，我会整理一份可执行的改造方案。",
          },
        ],
        mentions: [],
        intent: "progress",
        visibility: "public",
      },
    ],
    teamRuns: [
      {
        teamRunId: "team-run",
        groupId: "fixture-group",
        groupRevision: 1,
        revision: 1,
        goalMessageId: "message-goal",
        goal: "输出接口改造方案与验证结果",
        leaderMemberId: "leader",
        status: "running",
        dispatchSuspended: false,
        dispatchEpoch: 1,
        budget: {
          maxMembers: 8,
          maxConcurrent: 4,
          maxStarts: 32,
          startsUsed: 2,
          maxHops: 8,
        },
        createdAt: "2026-09-10T08:00:00Z",
        updatedAt: "2026-09-10T08:00:00Z",
      },
    ],
    tasks: [
      {
        taskId: "task-contract",
        groupId: "fixture-group",
        teamRunId: "team-run",
        revision: 1,
        title: "梳理接口契约",
        description: "检查请求字段、返回格式与错误处理。",
        ownerMemberId: "leader",
        dependencies: [],
        status: "succeeded",
        acceptanceCriteria: "列明受影响的 API 与兼容性约束。",
        attempts: [],
      },
      {
        taskId: "task-implementation",
        groupId: "fixture-group",
        teamRunId: "team-run",
        revision: 1,
        title: "验证实现边界",
        description: "验证运行时身份和审批引用不会串用。",
        ownerMemberId: "engineer",
        dependencies: ["task-contract"],
        status: "running",
        acceptanceCriteria: "通过两成员并发隔离测试。",
        attempts: [
          {
            attemptId: "attempt-1",
            attemptNumber: 1,
            executionEpoch: 1,
            status: "running",
            artifacts: [],
            startedAt: "2026-09-10T08:03:00Z",
          },
        ],
      },
      {
        taskId: "task-summary",
        groupId: "fixture-group",
        teamRunId: "team-run",
        revision: 1,
        title: "整理交付方案",
        description: "汇总结果并给出实施计划。",
        ownerMemberId: "leader",
        dependencies: ["task-implementation"],
        status: "blocked",
        reason: "等待实现验证通过验收",
        acceptanceCriteria: "完整可执行的方案文档。",
        attempts: [],
      },
    ],
    deliveries: [],
    interactions: [],
  };
}

export function memberRef(memberId = "engineer"): MemberStreamRef {
  return {
    authorityRef: "fixture-local",
    groupId: "fixture-group",
    memberId,
    bindingRef: `binding-${memberId}`,
    providerRef: "fixture-provider",
    sessionId: `session-${memberId}`,
    runId: `run-${memberId}`,
  };
}
export function teamEvent(
  seq: number,
  type = "message.created",
  payload: Record<string, unknown> = {},
): GroupEvent {
  return {
    apiVersion: TEAMS_API_VERSION,
    eventId: `event-${seq}`,
    groupId: "fixture-group",
    groupSeq: seq,
    type,
    createdAt: "2026-09-10T08:01:00Z",
    payload,
  };
}

export function teamExecution(snapshot = teamSnapshot()) {
  const source = memberRef();
  return {
    groupId: snapshot.group.groupId,
    teamRunId: snapshot.teamRuns[0].teamRunId,
    watermark: snapshot.watermark,
    nodes: [
      ...snapshot.tasks.map((task) => ({
        nodeId: task.taskId,
        kind: "task" as const,
        title: task.title,
        status: task.status,
        taskId: task.taskId,
        memberId: task.ownerMemberId,
      })),
      {
        nodeId: "run-node-engineer",
        kind: "run" as const,
        title: "工程师执行",
        status: "running",
        memberId: "engineer",
        source,
        taskId: "task-implementation",
        parentNodeId: "task-implementation",
      },
      {
        nodeId: "child-node-review",
        kind: "child_invocation" as const,
        title: "兼容性子检查",
        status: "running",
        memberId: "engineer",
        source,
        taskId: "task-implementation",
        parentNodeId: "run-node-engineer",
      },
    ],
    edges: [
      {
        source: "task-implementation",
        target: "run-node-engineer",
        kind: "invocation" as const,
      },
      {
        source: "run-node-engineer",
        target: "child-node-review",
        kind: "invocation" as const,
      },
    ],
  };
}

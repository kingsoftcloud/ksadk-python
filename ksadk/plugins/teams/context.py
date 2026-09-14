"""Small execution context, separate from the complete owner UI snapshot."""

from .domain import member_key


def execution_context(domain, actor, current_task_id=None):
    with domain.store.transaction() as tx:
        group = domain.authorize(tx, actor, actor.group_id)
        member = tx.get("member", member_key(actor.group_id, actor.member_id))
        run = tx.get("team_run", actor.team_run_id)
        tasks = tx.list("task", actor.group_id, team_run_id=actor.team_run_id)
        recent = tx.recent("message", actor.group_id, 12)
        artifacts = tx.recent("artifact", actor.group_id, 12)
        members = tx.list("member", actor.group_id)
    relevant = sorted(
        tasks,
        key=lambda task: (
            task["taskId"] != current_task_id,
            task["ownerMemberId"] != actor.member_id,
            task["status"] in {"succeeded", "failed", "cancelled"},
        ),
    )[:32]
    projected = []
    for task in relevant:
        record = {
            key: task.get(key)
            for key in (
                "taskId",
                "title",
                "status",
                "ownerMemberId",
                "revision",
                "dependencies",
            )
        }
        record["title"] = str(record["title"])[:120]
        if task["taskId"] == current_task_id:
            record["acceptanceCriteria"] = task["acceptanceCriteria"][:600]
        if task["attempts"]:
            attempt = task["attempts"][-1]
            record["attemptId"] = attempt["attemptId"]
            if attempt.get("result"):
                record["resultSummary"] = attempt["result"][:500]
            record["artifactIds"] = [item["artifactId"] for item in attempt["artifacts"]]
        projected.append(record)
    messages = []
    for message in recent:
        if message.get("visibility") == "internal" or message["senderName"] == "任务分派":
            continue  # The accepted task delivery already contains the complete brief.
        if (
            member["role"] != "leader"
            and message.get("mentions")
            and actor.member_id not in message["mentions"]
        ):
            continue
        parts = [
            {"kind": "text", "text": str(part.get("text", ""))[:500]}
            if part["kind"] == "text"
            else part
            for part in message["parts"]
        ]
        messages.append(
            {
                "messageId": message["messageId"],
                "senderName": message["senderName"],
                "parts": parts[:4],
                "intent": message["intent"],
            }
        )
    return {
        "memberId": actor.member_id,
        "role": member["role"],
        "teamRunId": run["teamRunId"],
        "goal": run["goal"][:1200],
        "currentTaskId": current_task_id,
        "taskAcceptance": group["_policy"]["taskAcceptance"],
        "members": [
            {key: member[key] for key in ("memberId", "name", "role")}
            for member in members
            if member["status"] == "active"
        ],
        "tasks": projected,
        "taskCount": len(tasks),
        "tasksTruncated": len(tasks) > len(projected),
        "recentMessages": messages[-4:],
        "artifacts": [
            {key: item[key] for key in ("artifactId", "name", "digest")} for item in artifacts
        ],
        "budget": run["budget"],
    }

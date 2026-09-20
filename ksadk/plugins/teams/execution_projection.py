"""Trusted, atomic projection of task-worker canonical completion.

No browser/model route is exposed. The Host verifies native canonical facts,
artifact authorization and source identity before calling this domain port.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .cloud_contracts import TerminalEvidence
from .cloud_contracts import digest as canonical_digest
from .contracts import TERMINAL
from .errors import TeamsError
from .store import Transaction, digest, now


class CompletionCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    result: str = Field(max_length=100_000, strict=True)
    artifacts: list[dict[str, Any]] = Field(default_factory=list, max_length=4096)


class CompletionUsage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    totalTokens: int = Field(ge=0, strict=True)


def completion_result_digest(candidate: CompletionCandidate | dict[str, Any]) -> str:
    """Hash the Host-normalized final result envelope, not intermediate events."""
    return canonical_digest(CompletionCandidate.model_validate(candidate).model_dump())


class ExecutionCompletionProjection:
    def _control_targets(
        self,
        tx: Transaction,
        run: dict[str, Any],
        *,
        include_terminal: bool = False,
    ) -> list[dict[str, Any]]:
        targets = {}
        for delivery in tx.list("delivery", run["groupId"], team_run_id=run["teamRunId"]):
            if (delivery.get("_terminalState") and not include_terminal) or delivery[
                "status"
            ] == "rejected":
                continue
            member = self.delivery_member(tx, delivery)
            grant_id = (
                delivery.get("_grantId")
                or "grant_" + digest([run["teamRunId"], delivery["memberId"]])[7:]
            )
            target_id = (
                "target_" + digest([member["bindingRef"], member["sessionId"], grant_id])[7:]
            )
            target = targets.setdefault(
                target_id,
                {
                    "targetId": target_id,
                    "memberId": member["memberId"],
                    "grantId": grant_id,
                    "member": deepcopy(member),
                    "deliveryIds": [],
                },
            )
            target["deliveryIds"].append(delivery["deliveryId"])
        return list(targets.values())

    def project_execution_completion(
        self,
        *,
        delivery_id: str,
        terminal_evidence: TerminalEvidence | dict[str, Any],
        status: Literal["succeeded", "failed", "cancelled", "interrupted"],
        candidate: CompletionCandidate | dict[str, Any] | None,
        usage: CompletionUsage | dict[str, Any] | None,
        projection_key: str,
        result_complete: bool = True,
    ) -> dict[str, Any]:
        evidence = TerminalEvidence.model_validate(terminal_evidence).model_dump(mode="json")
        candidate_value = (
            CompletionCandidate.model_validate(candidate).model_dump()
            if candidate is not None
            else None
        )
        usage_value = (
            CompletionUsage.model_validate(usage).model_dump() if usage is not None else None
        )
        if status not in {"succeeded", "failed", "cancelled", "interrupted"}:
            raise TeamsError("invalid_run_state", "完成投影必须包含真实终态", status=422)
        if type(result_complete) is not bool or (
            not result_complete and candidate_value is not None
        ):
            raise TeamsError("invalid_completion", "未读齐的结果不能提交候选", status=422)
        if result_complete and usage_value is None:
            raise TeamsError("usage_required", "完成投影需要最终用量", status=422)
        if candidate_value is not None and evidence["resultDigest"] != completion_result_digest(
            candidate_value
        ):
            raise TeamsError("result_digest_mismatch", "候选结果与终态摘要不一致", status=409)
        if result_complete and candidate_value is None and evidence["resultDigest"] is not None:
            raise TeamsError("result_pending", "终态声明的结果尚未读取完整", status=409)
        payload = {
            "evidence": evidence,
            "status": status,
            "candidate": candidate_value,
            "usage": usage_value,
            "resultComplete": result_complete,
        }

        def project(tx: Transaction) -> dict[str, Any]:
            delivery = tx.get("delivery", delivery_id)
            if delivery.get("_candidateMode") != "canonical_terminal" or not delivery.get(
                "_taskId"
            ):
                raise TeamsError(
                    "candidate_mode_mismatch", "此执行不允许宿主生成任务候选", status=409
                )
            if (
                delivery["_sessionId"] != evidence["sessionId"]
                or not delivery.get("commandId")
                or delivery["commandId"] != evidence["commandId"]
                or delivery.get("runId") not in {None, evidence["runId"]}
            ):
                raise TeamsError(
                    "run_identity_conflict", "终态必须对应原始执行命令和会话", status=409
                )
            previous = delivery.get("_completionEvidence")
            if previous:

                def core(value):
                    return {k: v for k, v in value.items() if k != "resultDigest"}

                if (
                    core(previous) != core(evidence)
                    or delivery["_nativeTerminalState"] != status
                    or (
                        previous.get("resultDigest") is not None
                        and evidence["resultDigest"] is not None
                        and previous["resultDigest"] != evidence["resultDigest"]
                    )
                ):
                    raise TeamsError(
                        "terminal_evidence_conflict", "原始执行终态证据发生冲突", status=409
                    )
            if delivery.get("_completionDigest"):
                if result_complete and delivery["_completionDigest"] != digest(payload):
                    raise TeamsError(
                        "completion_conflict", "执行已经用不同的结果完成结算", status=409
                    )
                return {
                    "status": delivery["_terminalState"],
                    "projectionKey": delivery["_completionKey"],
                }
            if delivery.get("_terminalState"):
                raise TeamsError("completion_conflict", "此执行已经由另一投影路径结算", status=409)
            delivery.update(
                runId=evidence["runId"],
                status="accepted",
                _runStatus=status,
                _completionEvidence=evidence,
                _nativeTerminalState=status,
                revision=delivery["revision"] + 1,
            )
            if not result_complete:
                delivery.update(resultState="pending", reason="result_pending")
                self.publish(tx, "delivery", delivery_id, delivery)
                task = tx.get("task", delivery["_taskId"])
                if task["attempts"][-1]["attemptId"] == delivery.get(
                    "_attemptId"
                ) and not delivery.get("_fenced"):
                    task["status"] = task["attempts"][-1]["status"] = "running"
                    task.update(
                        reason="等待读取完整执行结果",
                        reasonCode="result_pending",
                        revision=task["revision"] + 1,
                    )
                    self.publish(tx, "task", task["taskId"], task)
                member = self.delivery_member(tx, delivery)
                current_member = self.run_member(tx, delivery["teamRunId"], delivery["memberId"])
                if current_member["sessionId"] == member["sessionId"]:
                    member.update(
                        executionStatus="waiting",
                        activeRunId=evidence["runId"],
                        waitingReason="result_pending",
                        revision=current_member["revision"] + 1,
                    )
                    self.publish_run_member(tx, member)
                run = tx.get("team_run", delivery["teamRunId"])
                self.update_active_clock(tx, run)
                tx.put("team_run", run["teamRunId"], run)
                return {"status": "result_pending"}

            task = tx.get("task", delivery["_taskId"])
            latest_attempt = task["attempts"][-1]
            current_attempt = latest_attempt["attemptId"] == delivery.get(
                "_attemptId"
            ) and not delivery.get("_fenced")
            has_result = candidate_value is not None and (
                bool(candidate_value["result"].strip()) or bool(candidate_value["artifacts"])
            )
            if current_attempt and has_result and status == "succeeded":
                self._record_task_candidate(
                    tx,
                    task,
                    latest_attempt,
                    run_id=evidence["runId"],
                    result=candidate_value["result"],
                    artifacts=candidate_value["artifacts"],
                )
            elif current_attempt:
                latest_attempt["_candidate"] = None
                tx.put("task", task["taskId"], task)
            delivery.update(
                resultState="complete",
                reason=None,
                _completionDigest=digest(payload),
                _completionKey=f"terminal:{evidence['commandId']}:{evidence['terminalSeq']}",
                _completionCandidate=candidate_value,
                _completionUsage=usage_value,
            )
            tx.put("delivery", delivery_id, delivery)
            result = self._project_run(
                tx,
                delivery_id=delivery_id,
                run_id=evidence["runId"],
                status=status,
                output=candidate_value["result"] if candidate_value else "",
                tokens=usage_value["totalTokens"],
                completion_projection=True,
            )
            if current_attempt:
                task = tx.get("task", delivery["_taskId"])
                task.pop("reasonCode", None)
                if has_result and status == "succeeded":
                    task.pop("reason", None)
                elif status == "succeeded":
                    task.update(reasonCode="result_missing", reason="执行结束但没有结果或交付物")
                    run = tx.get("team_run", delivery["teamRunId"])
                    if run["status"] not in TERMINAL | {"cancel_requested"}:
                        run.update(
                            status="needs_attention",
                            reason="result_missing",
                            revision=run["revision"] + 1,
                            updatedAt=now(),
                        )
                        self.publish(tx, "team_run", run["teamRunId"], run)
                task["revision"] += 1
                self.publish(tx, "task", task["taskId"], task)
            return {**result, "projectionKey": delivery["_completionKey"]}

        return self.store.mutate(f"completion:{delivery_id}", projection_key, payload, project)

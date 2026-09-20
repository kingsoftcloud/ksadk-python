"""Atomic Leader identity changes inside the existing Teams transaction.

The Server supplies already verified offline/grant/release evidence. This mixin
owns only domain identity/outbox transitions, never provider or lease machinery.
"""

from copy import deepcopy

from .contracts import TERMINAL
from .errors import TeamsError
from .store import now


class LeaderDecisions:
    def fence_leader(self, tx, actor, run_id, *, expected_revision, expected_epoch, takeover_id):
        run = tx.get("team_run", run_id)
        self.authorize(tx, actor, run["groupId"], owner=True)
        if (
            run["revision"] != expected_revision
            or run.get("_leaderEpoch", 1) != expected_epoch
            or run["status"] in TERMINAL | {"cancel_requested"}
            or run["dispatchSuspended"]
        ):
            raise TeamsError("leader_takeover_conflict", "本轮状态已改变，无法接管", status=409)
        member = self.run_member(tx, run_id, run["leaderMemberId"])
        page = tx.list_page("delivery", run["groupId"], team_run_id=run_id, limit=1000)
        if page.next_cursor is not None:
            raise TeamsError("takeover_evidence_too_large", "原执行证据需完整核查", status=409)
        deliveries = []
        for row in page.items:
            if row["memberId"] != member["memberId"] or row.get("_terminalState"):
                continue
            row.setdefault("_memberSnapshot", deepcopy(self.delivery_member(tx, row)))
            row.update(_fenced=True, _takeoverId=takeover_id, revision=row["revision"] + 1)
            self.publish(tx, "delivery", row["deliveryId"], row)
            deliveries.append(row)
        run.update(
            _leaderEpoch=expected_epoch + 1,
            _leaderTakeoverId=takeover_id,
            revision=run["revision"] + 1,
            updatedAt=now(),
        )
        self.publish(tx, "team_run", run_id, run)
        return deepcopy(member), deliveries

    def activate_leader(self, tx, actor, takeover, checkpoint, binding):
        run = tx.get("team_run", takeover["teamRunId"])
        self.authorize(tx, actor, run["groupId"], owner=True)
        if (
            run.get("_leaderTakeoverId") != takeover["takeoverId"]
            or run.get("_leaderEpoch") != takeover["newLeaderEpoch"]
            or run["status"] in TERMINAL | {"cancel_requested"}
            or run["dispatchSuspended"]
        ):
            raise TeamsError("leader_takeover_conflict", "本轮状态已改变，无法接管", status=409)
        if takeover.get("newDeliveryId"):
            return tx.get("delivery", takeover["newDeliveryId"])
        member = deepcopy(self.run_member(tx, run["teamRunId"], run["leaderMemberId"]))
        member.update(
            bindingRef=binding["bindingRef"],
            binding=deepcopy(binding),
            sessionId=takeover["newSessionId"],
            revision=member["revision"] + 1,
            executionStatus="idle",
            activeRunId=None,
        )
        member["binding"]["memberClass"] = "full_member"
        # Historical deliveries retain their original snapshot; the logical
        # member and TeamRun remain, only its new execution identity changes.
        self.publish(tx, "run_member", member["runMemberId"], member)
        run["_roster"] = [
            deepcopy(member) if m["memberId"] == member["memberId"] else m for m in run["_roster"]
        ]
        self.publish(tx, "team_run", run["teamRunId"], run)
        message = {
            "messageId": takeover["checkpointMessageId"],
            "groupId": run["groupId"],
            "teamRunId": run["teamRunId"],
            "revision": 1,
            "senderPrincipal": self.authority_ref,
            "senderName": "团队协调服务",
            "groupRole": "owner",
            "memberId": None,
            "mentions": [member["memberId"]],
            "intent": "directed",
            "replyTo": None,
            "visibility": "internal",
            "workspace": None,
            "parts": [{"kind": "text", "text": checkpoint}],
            "createdAt": now(),
            "_checkpointRef": takeover["checkpointRef"],
        }
        self.publish(tx, "message", message["messageId"], message, created=True)
        delivery = self.dispatch(
            tx,
            run,
            message,
            member["memberId"],
            source=takeover["takeoverId"],
            purpose="leader_takeover",
        )
        if delivery is None:
            raise TeamsError(
                "leader_dispatch_blocked", "新协调者启动受预算或本轮状态限制", status=409
            )
        return delivery

"""Stage and consume host-authorized child lifecycle commands for a parent run."""

from __future__ import annotations

from .state import CommandReceipt, RunCommand, RunSnapshot


class KernelChildControl:
    """Stage and consume host-authorized child lifecycle commands for a parent run."""

    def child_tasks(self, run_id: str) -> tuple[dict, ...]:
        snapshot = self.snapshot(run_id)
        reader = getattr(self._child_promotion, "child_tasks", None)
        if not callable(reader):
            raise ValueError("child_directory_unavailable")
        rows = reader(snapshot)
        if self.configuration.child_promotion_policy is None:
            rows = tuple({**row, "can_promote": False} for row in rows)
        if self.configuration.child_input_policy is None:
            rows = tuple({**row, "can_send_input": False} for row in rows)
        if not self.supports_child_cancellation():
            rows = tuple({**row, "can_cancel": False} for row in rows)
        return rows

    def supports_child_cancellation(self) -> bool:
        return self.configuration.child_lifecycle_policy == "owned-background/v2" and all(
            callable(getattr(self._child_promotion, name, None))
            for name in ("can_cancel", "cancel_child")
        )

    def cancel_child(self, run_id: str, *, child_run_id: str, command_id: str):
        from .child_promotion import ChildPromotionInput

        payload = ChildPromotionInput(child_run_id=child_run_id)
        snapshot = self.snapshot(run_id)
        if not self.supports_child_cancellation() or not any(
            row["child_run_id"] == child_run_id
            for row in self._child_promotion.child_tasks(snapshot)
        ):
            return CommandReceipt(
                command_id=command_id,
                status="rejected",
                reason="child_not_owned_or_cancellation_unavailable",
            )
        return self.submit(
            run_id,
            RunCommand(
                command_id=command_id, kind="cancel_child", payload=payload.model_dump(mode="json")
            ),
        )

    async def _consume_child_cancellation(self, run_id: str, command: RunCommand):
        from .child_cancellation import consume_child_cancellation

        return await consume_child_cancellation(
            self.snapshot(run_id),
            command,
            enabled=self.supports_child_cancellation(),
            port=self._child_promotion,
            commit=self._commit,
        )

    def child_execution_view(self, run_id: str, child_run_id: str) -> dict:
        reader = getattr(self._child_promotion, "child_execution_view", None)
        if not callable(reader):
            raise ValueError("child_execution_view_unavailable")
        return reader(self.snapshot(run_id), child_run_id)

    async def release_children(self, run_id: str) -> None:
        from .child_lifecycle_contract import ChildResourceReleaseRequest

        # Reading the pinned Run does not reacquire a lost parent lease.
        # Cleanup stops locally owned child tasks; it cannot terminalize or
        # reconcile facts under a replacement parent's fence.
        self.snapshot(run_id)
        if self.configuration.child_release_policy is not None:
            await self._child_promotion.release_run(
                ChildResourceReleaseRequest(scope=self.scope, run_id=run_id)
            )

    def promote_child(self, run_id: str, *, child_run_id: str, command_id: str):
        from .child_promotion import ChildPromotionInput

        payload = ChildPromotionInput(child_run_id=child_run_id)
        if self.configuration.child_promotion_policy is None:
            return CommandReceipt(
                command_id=command_id,
                status="rejected",
                reason="child_promotion_policy_unavailable",
            )
        return self.submit(
            run_id,
            RunCommand(
                command_id=command_id, kind="promote_child", payload=payload.model_dump(mode="json")
            ),
        )

    def _consume_child_promotion(self, run_id: str, command: RunCommand) -> RunSnapshot:
        from .child_promotion import consume_promotion

        return consume_promotion(
            self.snapshot(run_id),
            command,
            port=self._child_promotion,
            enabled=self.configuration.child_promotion_policy is not None,
            commit=self._commit,
        )

    def send_child_input(self, run_id, *, child_run_id, text, command_id, expected_goal_revision):
        from .child_input import ChildInput

        payload = ChildInput(
            child_run_id=child_run_id, text=text, expected_goal_revision=expected_goal_revision
        )
        if self.configuration.child_input_policy is None:
            return CommandReceipt(
                command_id=command_id, status="rejected", reason="child_input_policy_unavailable"
            )
        return self.submit(
            run_id,
            RunCommand(
                command_id=command_id, kind="message_child", payload=payload.model_dump(mode="json")
            ),
        )

    def _consume_child_input(self, run_id, command):
        from .child_input_runtime import consume_child_input

        return consume_child_input(
            self.snapshot(run_id),
            command,
            port=self._child_promotion,
            enabled=self.configuration.child_input_policy is not None,
            commit=self._commit,
        )

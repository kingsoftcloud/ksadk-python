"""Session admission, notification turns and cumulative task-chain budget authority."""

from __future__ import annotations

import hashlib
from uuid import uuid4

from ksadk.harness.stores.native_contract import (
    ForkingRunStore,
    NotificationTurnCreation,
    StoreConflict,
    check_scope,
)

from .completion import TaskContract
from .fork import ForkCreation, build_fork_snapshot, command_run_id
from .history import project_session_history
from .ports import digest, validate_task_tool_scope
from .run_clock import wall_budget_stop
from .state import CommitBatch, RunPhase, RunSnapshot
from .transitions import commit_transition as commit_transition
from .transitions import finish as finish
from .user_input import NativeUserInput


class KernelRunAdmission:
    """Session admission, notification turns and cumulative task-chain budget authority."""

    def start(
        self,
        *,
        goal: str,
        session_id: str,
        run_id: str | None = None,
        origin: dict | None = None,
        task_contract: TaskContract | None = None,
        expected_parent_run_id: str | None = None,
        pin_parent: bool = False,
        user_input: NativeUserInput | None = None,
        task_chain_id: str | None = None,
    ) -> RunSnapshot:
        config = self.configuration
        if user_input is not None:
            user_input = NativeUserInput.model_validate(user_input)
            if user_input.goal != goal:
                raise ValueError("native_user_input_goal_mismatch")
            if any(document.full_text is not None for document in user_input.documents):
                raise ValueError("native_user_document_capture_required")
            user_input.check_profile(config.profile)
        contract = task_contract or config.contract
        validate_task_tool_scope(contract, config.tools)
        parent = self.store.session_head(self.scope, session_id)
        if pin_parent and (parent.run_id if parent else None) != expected_parent_run_id:
            raise StoreConflict("session changed while compiling task contract")
        if parent is not None and not parent.phase.terminal:
            raise StoreConflict("session already has an active turn")
        history = project_session_history(
            parent,
            profile_digest=config.profile.digest,
            configuration_digest=config.digest,
        )
        artifact_refs = list(parent.artifact_refs) if parent else []
        available = {ref["uri"]: ref for ref in artifact_refs}
        for document in user_input.documents if user_input is not None else ():
            if document.text_artifact is not None:
                ref = document.text_artifact.model_dump(mode="json")
                if ref["uri"] in available and available[ref["uri"]] != ref:
                    raise ValueError("native_user_document_artifact_conflict")
                if ref["uri"] not in available:
                    artifact_refs.append(ref)
                    available[ref["uri"]] = ref
        snapshot = RunSnapshot(
            scope=self.scope,
            run_id=run_id or str(uuid4()),
            session_id=session_id,
            turn_id=str(uuid4()),
            parent_run_id=parent.run_id if parent else None,
            goal=goal,
            initial_goal_digest=hashlib.sha256(goal.encode("utf-8")).hexdigest(),
            messages=(
                *history,
                *(
                    user_input.messages()
                    if user_input is not None
                    else ({"role": "user", "content": goal},)
                ),
            ),
            background_commands=parent.background_commands if parent else (),
            task_progress=parent.task_progress if parent else (),
            task_progress_revision=parent.task_progress_revision if parent else 0,
            workspace_revision=(
                parent.workspace_revision
                if parent
                else (
                    self.context.workspace_revision
                    if self.context.workspace_revision != "unavailable"
                    else None
                )
            ),
            artifact_refs=tuple(artifact_refs),
            workspace_changed_paths=parent.workspace_changed_paths if parent else (),
            workspace_observation_status=(
                parent.workspace_observation_status
                if parent
                else (
                    "initial" if self.context.workspace_revision != "unavailable" else "unavailable"
                )
            ),
            workspace_observation_reason=(parent.workspace_observation_reason if parent else None),
            task_contract=contract.model_dump(mode="json"),
            context_binding=self.context.binding,
            profile_digest=config.profile.digest,
            contract_digest=digest(contract),
            configuration_digest=config.digest,
            origin=origin or {},
            activation_resource_ref=(
                self._activation_resource.model_dump(mode="json")
                if self._activation_resource is not None
                else None
            ),
            task_chain_id=task_chain_id,
        )
        from .context_session_history import inherit_session_summary

        inherited_summary = inherit_session_summary(
            parent,
            snapshot,
            config,
            self.context,
            imported_count=len(history),
        )
        if inherited_summary is not None:
            snapshot = snapshot.model_copy(update={"compaction_history": inherited_summary})
        self.store.create_turn(snapshot, expected_parent_run_id=parent.run_id if parent else None)
        self.attach(snapshot.run_id)
        return self.snapshot(snapshot.run_id)

    def fork(
        self,
        *,
        source_run_id: str,
        expected_source_revision: int,
        session_id: str,
        goal: str,
        command_id: str | None = None,
        run_id: str | None = None,
        origin: dict | None = None,
        task_contract: TaskContract | None = None,
    ) -> ForkCreation:
        """Start a new branch at an immutable terminal Run, not its latest head.

        New configuration comes from this destination host. Historical execution
        state and resources remain with the source; this does not rewind files,
        resume an interrupted side effect, or import another runtime's snapshot.
        A stable command_id returns the original branch on retries; only a
        created receipt attaches execution ownership. Without an ID each call
        is a new command, subject to the unique destination Session boundary.
        """
        if not isinstance(self.store, ForkingRunStore):
            raise StoreConflict("store does not support atomic fork creation")
        if type(expected_source_revision) is not int or expected_source_revision < 0:
            raise StoreConflict("fork source revision must be a nonnegative integer")
        source = self.store.load(self.scope, source_run_id)
        check_scope(self.scope, source)
        if not source.phase.terminal:
            raise StoreConflict("fork requires a terminal source run")
        if source.revision != expected_source_revision:
            raise StoreConflict("fork source revision changed")
        contract = task_contract or self.configuration.contract
        validate_task_tool_scope(contract, self.configuration.tools)
        if command_id is not None:
            if not isinstance(command_id, str) or not command_id.strip() or len(command_id) > 160:
                raise StoreConflict("fork command ID must be nonempty and at most 160 characters")
            addressed_run = command_run_id(self.scope, source_run_id, command_id)
            if run_id is not None and run_id != addressed_run:
                raise StoreConflict("fork command must use its scoped Run identity")
            run_id = addressed_run
        snapshot = build_fork_snapshot(
            source,
            run_id=run_id or str(uuid4()),
            turn_id=str(uuid4()),
            session_id=session_id,
            goal=goal,
            configuration=self.configuration,
            contract=contract,
            context_binding=self.context.binding,
            workspace_revision=self.context.workspace_revision,
            activation_resource=self._activation_resource,
            origin=origin,
            command_id=command_id,
        )
        result = self.store.create_fork(snapshot)
        if result.created:
            self.attach(result.snapshot.run_id)
        return result

    def start_notification_turn(
        self,
        *,
        source_run_id: str,
        task_id: str,
        task_contract: TaskContract | None = None,
    ) -> NotificationTurnCreation:
        """Admit a background-notification turn in the source run's session.

        Creates a new run in the *same session* with a deterministic run id, inheriting
        the source's goal (task continuity), background commands and task-chain identity.
        No synthetic user prompt is added — the kernel's ``command_probe`` injects the
        ``background_command_observation`` message at the PREPARING_CONTEXT boundary.

        Pre-checks: source must be terminal; the task must exist in the source's
        background commands and not already be notified; the source must not be
        canceled/failed. Repeat admissions with a matching request digest return the
        already-created turn and re-attach it so it can be driven (not a permanent
        stub); a different request for the same task_id is rejected by the store.
        Never revives a terminal run or reuses an unconfirmed effect.
        """
        from .notification_admission import (
            build_notification_snapshot,
            notification_request_command,
            notification_run_id,
        )

        source = self.store.load(self.scope, source_run_id)
        check_scope(self.scope, source)
        if not source.phase.terminal:
            raise StoreConflict("notification turn requires a terminal source run")
        # The task must actually exist in the source run's owned background commands.
        if not any(item.ref.task_id == task_id for item in source.background_commands):
            raise StoreConflict("background_command_task_not_found")
        if source.phase in {RunPhase.CANCELED, RunPhase.INTERRUPTED, RunPhase.FAILED}:
            raise StoreConflict("notification source run is canceled or failed")
        contract = task_contract or self.configuration.contract
        validate_task_tool_scope(contract, self.configuration.tools)
        run_id = notification_run_id(self.scope, source_run_id, task_id)
        head = self.store.session_head(self.scope, source.session_id)
        # Idempotent re-admission: a notification turn with this deterministic run
        # id may already exist in the durable store. A repeat admission (retry or
        # crash-recovery) must re-attach and re-drive the existing run rather than
        # rebuild its snapshot (``project_session_history`` requires a terminal
        # parent and fails for a non-terminal head — the crash window between the
        # durable commit and the driver finishing, where the run may be at
        # PREPARING_CONTEXT with the observation staged but no model intent yet).
        # This fastpath runs BEFORE the already-notified pre-check: a staged
        # observation sets ``notified=True`` on the run's background commands
        # before the model turn, so the head (which is this unfinished run) would
        # trip the already-notified check and discard an unfinished turn. The
        # existing run is recovered instead — re-driven to the model boundary by
        # the caller's ``register_and_drive_notification_turn``. ``notified`` on a
        # recovered run is the exactly-once observation ledger; the kernel's
        # ``command_probe`` does not re-inject already-notified commands, so the
        # model receives the staged observation exactly once.
        try:
            existing = self.store.load(self.scope, run_id)
        except KeyError:
            existing = None
        if existing is not None:
            # Validate the request identity on the attach path: the immutable
            # admission request (scope/session/source/task/configuration/profile/
            # contract/chain/origin) must match the existing turn. A changed
            # explicit task_contract (or any other request-identity field) is
            # rejected — mirroring ``duplicate_notification_turn``'s request-digest
            # contract, without rebuilding the snapshot from a nonterminal head.
            proposed_command = notification_request_command(
                source,
                task_id=task_id,
                configuration=self.configuration,
                contract=contract,
                context_binding=self.context.binding,
                parent_run_id=head.run_id if head else source_run_id,
                snapshot_source=head if head is not None else source,
            )
            if existing.notification_command != proposed_command:
                raise StoreConflict("notification turn was already used with another request")
            self.attach(existing.run_id)
            return NotificationTurnCreation(snapshot=existing, created=False)
        # No existing run: the already-notified pre-check now guards against a
        # task a prior (terminal) notification turn already delivered, when that
        # turn's run is not found by the deterministic id (a durable-store
        # inconsistency safety net). Check the CURRENT head's background_commands
        # (cumulative notified state from prior notification turns).
        notified_source = head if head is not None else source
        if any(
            item.ref.task_id == task_id and item.notified
            for item in notified_source.background_commands
        ):
            raise StoreConflict("background_command_already_notified")
        # A public/manual admission must meet the same real completion gate as
        # the idle coordinator. Unavailable observations and a still-running
        # command retain the watch; they must not consume its dedup marker.
        from .background_command_contract import BackgroundCommandRequest

        refs = tuple(item.ref for item in source.background_commands if item.ref.task_id == task_id)
        observations = (
            self._background_commands.observe(
                BackgroundCommandRequest(
                    scope=self.scope,
                    run_id=source.run_id,
                    session_id=source.session_id,
                    refs=refs,
                )
            )
            if self._background_commands is not None
            else ()
        )
        if not any(
            item.ref == refs[0]
            and item.status
            in {
                "completed",
                "failed",
                "cancelled",
                "timed_out",
                "output_limit",
            }
            for item in observations
        ):
            raise StoreConflict("background_command_not_terminal")
        # The parent for CAS is the current session head, NOT the original source
        # run. After the first notification turn moves the head, subsequent
        # notifications for other tasks in the same source must CAS against the
        # current head (the prior notification turn), not the original source.
        expected_parent = head.run_id if head else source_run_id
        # Build the snapshot from the CURRENT head (not the original source) so
        # that background_commands carry the cumulative notified state from prior
        # notification turns. The original source is used only for the
        # task-existence and already-notified pre-checks above, and for the
        # notification_command's source_run_id binding.
        snapshot_source = head if head is not None else source
        snapshot = build_notification_snapshot(
            source,
            run_id=run_id,
            turn_id=str(uuid4()),
            task_id=task_id,
            configuration=self.configuration,
            contract=contract,
            context_binding=self.context.binding,
            workspace_revision=self.context.workspace_revision,
            parent_run_id=expected_parent,
            snapshot_source=snapshot_source,
        )
        result = self.store.create_notification_turn(
            snapshot,
            expected_parent_run_id=expected_parent,
        )
        # Always attach (both created and idempotent repeat) so the turn can be
        # driven. A repeat admission re-enters the existing run, which the driver
        # resumes from its committed phase — not a permanent stub.
        self.attach(result.snapshot.run_id)
        return result

    def notified_task_ids(self, run_id: str) -> set[str]:
        """Return the set of task_ids already delivered in a run's ledger."""
        snapshot = self.snapshot(run_id)
        return {item.ref.task_id for item in snapshot.background_commands if item.notified}

    def is_run_canceled(self, run_id: str) -> bool:
        """Authoritative terminal-status query; replaces event-store inference."""
        snapshot = self.snapshot(run_id)
        return snapshot.phase in {RunPhase.CANCELED, RunPhase.INTERRUPTED, RunPhase.FAILED}

    def task_chain_remaining_budget(self, run_id: str) -> int | None:
        """Remaining model-call budget for the run's task chain, or None if unbounded.

        A notification turn inherits the source run's chain, so its budget accrues to
        the original task. Returns None when the configuration has no explicit
        ``max_model_calls`` (ordinary unbounded runs). A finite budget subtracts the
        chain's settled usage; the result is never negative (0 = exhausted).
        """
        status = self.task_chain_budget_status(run_id)
        return status["model_calls_remaining"]

    def task_chain_budget_status(self, run_id: str) -> dict:
        """Remaining budget for the run's task chain across all dimensions.

        Returns a dict with ``model_calls_remaining``, ``tool_calls_remaining``,
        ``wall_seconds_remaining`` — each None if that dimension is unbounded, or a
        non-negative int/float if bounded (0 = exhausted). Raises StoreConflict
        if a finite chain has no ledger or a read error (fail-closed).
        """
        snapshot = self.snapshot(run_id)
        if not snapshot.task_chain_id:
            return {
                "model_calls_remaining": None,
                "tool_calls_remaining": None,
                "wall_seconds_remaining": None,
            }
        limits = self.configuration.limits
        if (
            limits.max_model_calls is None
            and limits.max_tool_calls is None
            and limits.max_wall_seconds is None
        ):
            return {
                "model_calls_remaining": None,
                "tool_calls_remaining": None,
                "wall_seconds_remaining": None,
            }
        usage = getattr(self.store, "task_chain_usage", None)
        if usage is None:
            raise StoreConflict(
                "finite task-chain budget requires a usage ledger; store does not support one"
            )
        try:
            chain = usage(self.scope, snapshot.task_chain_id)
        except Exception as exc:
            raise StoreConflict("task-chain usage read failed") from exc
        # If the current run is already settled, its usage is in chain totals.
        settled_ids = getattr(self.store, "chain_settled_run_ids", None)
        if callable(settled_ids):
            try:
                already_settled = snapshot.run_id in settled_ids(self.scope, snapshot.task_chain_id)
            except Exception as exc:
                raise StoreConflict("chain settled-runs read failed") from exc
        else:
            raise StoreConflict("chain settled-runs read failed")
        if already_settled:
            chain_model = chain.model_calls + chain.descendant_model_calls
            chain_tool = chain.tool_calls + chain.descendant_tool_calls
            chain_wall = chain.execution_elapsed_seconds
        else:
            chain_model = (
                chain.model_calls
                + snapshot.model_calls
                + chain.descendant_model_calls
                + snapshot.descendant_model_calls
            )
            chain_tool = (
                chain.tool_calls
                + snapshot.tool_calls
                + chain.descendant_tool_calls
                + snapshot.descendant_tool_calls
            )
            chain_wall = chain.execution_elapsed_seconds + snapshot.execution_elapsed_seconds
        return {
            "model_calls_remaining": (
                None
                if limits.max_model_calls is None
                else max(0, limits.max_model_calls - chain_model)
            ),
            "tool_calls_remaining": (
                None
                if limits.max_tool_calls is None
                else max(0, limits.max_tool_calls - chain_tool)
            ),
            "wall_seconds_remaining": (
                None
                if limits.max_wall_seconds is None
                else max(0.0, limits.max_wall_seconds - chain_wall)
            ),
        }

    def _check_chain_budget_exhausted(self, snapshot: RunSnapshot) -> CommitBatch | None:
        """Check if the task chain's cumulative model/wall budget is exhausted.

        Returns a terminal CommitBatch if the chain's model or wall budget is
        confirmed exhausted, or None otherwise. Uses the already-loaded
        snapshot — does not reload from store.

        The tool dimension is NOT checked here: the chain's already-consumed
        tool budget is folded into the effective per-run tool limit by
        ``effective_configuration``, so the canonical per-call boundary
        (``tools_exhausted`` / ``model_turn_control``) enforces it with *soft
        finalization* — a tools-spent chain admits no tools but the model still
        produces a final answer (preserving final-submission/reserve). A hard
        finish here would bypass that lawful finalization.

        Model exhaustion hard-finishes only when the chain has *actually
        consumed* model calls (chain_model > 0) and reached the cap — no answer
        is possible once the model budget is spent. A finite model budget with
        zero consumption is terminated at the PREPARING_CONTEXT boundary by the
        per-call ``model_budget_exhausted`` check, not here. Wall budget has no
        per-call boundary and is checked unconditionally (elapsed time is real
        consumption, never a zero-with-no-consumption case).

        A finite chain with a missing or unreadable ledger is a hard failure:
        the run cannot be admitted because the host cannot prove it has budget
        remaining. Read errors raise StoreConflict (fail-closed), not silently
        granted. A new task starts with a zero ledger created atomically by the
        in-commit settlement; a missing ledger for an existing finite chain is
        not the same as zero consumption.
        """
        if not snapshot.task_chain_id:
            return None
        max_model = self.configuration.limits.max_model_calls
        max_tool = self.configuration.limits.max_tool_calls
        max_wall = self.configuration.limits.max_wall_seconds
        if max_model is None and max_tool is None and max_wall is None:
            return None  # fully unbounded — no check
        chain = getattr(self.store, "task_chain_usage", None)
        if chain is None:
            raise StoreConflict(
                "finite task-chain budget requires a usage ledger; store does not support one"
            )
        try:
            usage = chain(self.scope, snapshot.task_chain_id)
        except Exception as exc:
            raise StoreConflict("task-chain usage read failed") from exc
        # The chain ledger entry is created atomically at create_turn time.
        # task_chain_usage raises StoreConflict if the entry is missing (deleted/
        # corrupted). A registered zero entry is valid — a new chain that has not
        # consumed anything yet. No heuristic scan needed.
        # If the current run is already settled, its usage is already in the chain
        # totals — do NOT add snapshot.model_calls again (would double-count).
        # If not yet settled, add the current run's unssettled contribution.
        # A read error from chain_settled_run_ids is NOT the same as "not settled" —
        # it means we cannot prove the settlement status. Fail-closed: raise.
        settled_ids = getattr(self.store, "chain_settled_run_ids", None)
        if callable(settled_ids):
            try:
                already_settled = snapshot.run_id in settled_ids(self.scope, snapshot.task_chain_id)
            except Exception as exc:
                raise StoreConflict("chain settled-runs read failed") from exc
        else:
            raise StoreConflict("chain settled-runs read failed")
        if already_settled:
            chain_model = usage.model_calls + usage.descendant_model_calls
            chain_wall = usage.execution_elapsed_seconds
        else:
            chain_model = (
                usage.model_calls
                + snapshot.model_calls
                + usage.descendant_model_calls
                + snapshot.descendant_model_calls
            )
            chain_wall = usage.execution_elapsed_seconds + snapshot.execution_elapsed_seconds
        if max_model is not None and chain_model > 0 and max_model - chain_model <= 0:
            return commit_transition(
                snapshot,
                finish(snapshot, "budget_exhausted", "chain_model_calls_exhausted"),
                "budget.exhausted",
                {"reason": "chain_model_calls_exhausted"},
            )
        # The tool dimension is NOT hard-finished here. The chain's already-
        # consumed tool budget is folded into the effective per-run tool limit
        # by effective_configuration, so the canonical per-call boundary
        # (tools_exhausted / model_turn_control) enforces it with soft
        # finalization: a tools-spent chain admits no tools but the model still
        # produces a final answer. A hard finish here would bypass the model's
        # final-submission/reserve path. The model dimension above is the only
        # hard finish (no answer is possible once the model budget is spent).
        if max_wall is not None and max_wall - chain_wall <= 0:
            return wall_budget_stop(
                snapshot,
                self.store,
                self.scope,
                elapsed=self._clock.elapsed(snapshot),
            )
        return None

    def _chain_wall_remaining(self, snapshot: RunSnapshot) -> float | None:
        """Remaining wall budget including the chain's prior settled elapsed time.

        The per-run clock's ``remaining`` only subtracts this run's own elapsed
        time from ``max_wall_seconds``. A continuation in a chain that already
        consumed wall time must have its remaining budget reduced by the chain's
        prior settled elapsed (parent + descendant), minus this run's own
        already-settled contribution (to avoid double-counting). The current
        run's own elapsed is already in ``_clock.remaining``; we additionally
        subtract the prior chain elapsed so the cumulative does not exceed the
        cap.
        """
        max_wall = self.configuration.limits.max_wall_seconds
        if max_wall is None:
            return None
        per_run_remaining = self._clock.remaining(snapshot)
        if per_run_remaining is None:
            return None
        if not snapshot.task_chain_id:
            return per_run_remaining
        chain_usage = getattr(self.store, "task_chain_usage", None)
        if chain_usage is None:
            raise StoreConflict("finite task-chain budget requires a usage ledger")
        try:
            usage = chain_usage(self.scope, snapshot.task_chain_id)
        except Exception:
            # Fail-closed: cannot prove cumulative wall budget remaining.
            # Do NOT return 0.0 (that fabricates exhaustion and stops the run
            # with budget_exhausted despite real budget left). Raise so the
            # caller blocks/retries rather than recording a fake terminal.
            raise StoreConflict("chain wall usage read failed")
        settled_ids = getattr(self.store, "chain_settled_run_ids", None)
        if callable(settled_ids):
            try:
                already_settled = snapshot.run_id in settled_ids(self.scope, snapshot.task_chain_id)
            except Exception as exc:
                raise StoreConflict("chain settled-runs read failed") from exc
        else:
            raise StoreConflict("chain settled-runs read failed")
        if already_settled:
            prior_elapsed = usage.execution_elapsed_seconds - snapshot.execution_elapsed_seconds
        else:
            prior_elapsed = usage.execution_elapsed_seconds
        # The per-run clock already accounts for this run's own elapsed; subtract
        # only the prior chain elapsed (settled runs of other runs in the chain).
        return (
            max_wall
            - prior_elapsed
            - self._clock.elapsed(snapshot)
            - snapshot.model_recovery_debt_seconds
        )

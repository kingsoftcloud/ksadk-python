"""Build-pinned source-page budget, shared by parent and child tool ports.

The complete declared batch shares one source allowance even when dispatched
in several parallel effects. Older history is left to ContextPlanner; this
policy bounds new source production rather than truncating sealed receipts.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from typing import Callable

from ksadk.harness.artifact_adapters.native_page_budget import (
    ArtifactPageBudget,
    ArtifactPageCapacityError,
    ArtifactPagePolicy,
)
from ksadk.harness.artifact_adapters.native_tool import ARTIFACT_READ_DEFINITION
from ksadk.harness.native.artifact_catalog_contract import ARTIFACT_CATALOG_POLICY
from ksadk.harness.native.artifact_partial_read_capacity import (
    PARTIAL_ARTIFACT_CAPACITY_POLICY,
    partial_artifact_group_admission,
)
from ksadk.harness.native.artifact_read_capacity import (
    ARTIFACT_CAPACITY_POLICY,
    artifact_group_capacity,
)
from ksadk.harness.native.artifact_single_read_capacity import (
    SINGLE_ARTIFACT_CAPACITY_POLICY,
    single_artifact_group_admission,
)
from ksadk.harness.native.context_driver import ContextDriver
from ksadk.harness.native.context_request_projection import FINALIZATION_READER_PROJECTION_POLICY
from ksadk.harness.native.context_runtime import ContextPlanner
from ksadk.harness.native.model import ModelProfile
from ksadk.harness.native.ports import RunConfiguration, ToolDefinition, digest
from ksadk.harness.native.state import RunSnapshot
from ksadk.harness.native.tool_batch_capacity import source_request_identity
from ksadk.tools.artifact_observation import (
    ARCHIVE_MODEL_POLICY,
    ARCHIVE_SEARCH_MODEL_POLICY,
    ARCHIVE_URI_MAX_CHARS,
    CANONICAL_ARCHIVE_MODEL_POLICY,
    OBSERVED_ARCHIVE_MODEL_POLICY,
    SOURCE_ARCHIVE_MODEL_POLICY,
)
from ksadk.toolsets.workspace_model_observation import WORKSPACE_SOURCE_MODEL_VERSION
from ksadk.toolsets.workspace_read_budget import SourceReadBudget


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True)
class StudioSourceReadPolicy:
    profile: ModelProfile
    count_units: Callable[[str], int]
    counter_id: str
    count_message_units: Callable[[dict], int] | None = None
    retain_requested_source: bool = False
    artifact_only_fallback: bool = False
    archive_model_policy: str = ARCHIVE_MODEL_POLICY
    message_projection_id: str | None = None
    artifact_pages: bool = False
    artifact_result_max_bytes: int | None = None
    context_planner: ContextPlanner | None = None

    def __post_init__(self):
        if self.context_planner is not None and (
            not isinstance(self.context_planner, ContextPlanner)
            or self.artifact_result_max_bytes is None
        ):
            raise ValueError("source_read_context_planner_invalid")
        if type(self.artifact_pages) is not bool:
            raise ValueError("source_read_artifact_policy_invalid")
        if self.artifact_result_max_bytes is not None and (
            not self.artifact_pages or type(self.artifact_result_max_bytes) is not int
            or not 1024 <= self.artifact_result_max_bytes <= 4 * 1024 * 1024
        ):
            raise ValueError("source_read_artifact_result_limit_invalid")
        if self.message_projection_id not in {None, "provider-message-fields/v1"} or (
            self.message_projection_id is not None and not callable(self.count_message_units)
        ):
            raise ValueError("source_read_message_projection_invalid")
        if self.archive_model_policy not in {
            ARCHIVE_MODEL_POLICY, ARCHIVE_SEARCH_MODEL_POLICY, CANONICAL_ARCHIVE_MODEL_POLICY,
            OBSERVED_ARCHIVE_MODEL_POLICY,
            SOURCE_ARCHIVE_MODEL_POLICY,
        } or (
            self.archive_model_policy in {
                ARCHIVE_SEARCH_MODEL_POLICY, CANONICAL_ARCHIVE_MODEL_POLICY,
                OBSERVED_ARCHIVE_MODEL_POLICY,
                SOURCE_ARCHIVE_MODEL_POLICY,
            }
            and not (self.retain_requested_source and self.artifact_only_fallback)
        ):
            raise ValueError("source_read_archive_policy_invalid")

    @property
    def digest(self):
        value = {
            "version": "studio-source-page-budget/v3",
            "counter_id": self.counter_id,
            "profile": self.profile.model_dump(mode="json"),
            "source_share": 0.5,
            "batch": "original_declared_call_group",
            "scope": "sdk_builtin_workspace_reader",
            "result_framing": WORKSPACE_SOURCE_MODEL_VERSION,
            "working_state_reserve": "profile_output_ceiling",
            "fixed_cost": "complete_prepared_model_surface",
            "media_framing": self.profile.image_input_mode,
        }
        if self.retain_requested_source:
            value.update(
                version="studio-source-page-budget/v4",
                archive_policy="archived-model-view/v1",
                source_capture="original_requested_bytes/v1",
            )
        if self.artifact_only_fallback:
            value.update(version="studio-source-page-budget/v5", overflow="saved_unread_source/v1")
        if self.archive_model_policy == ARCHIVE_SEARCH_MODEL_POLICY:
            value.update(
                version="studio-source-page-budget/v6",
                archive_policy=self.archive_model_policy,
                working_state_reserve="already_counted_continuity_plus_prospective_refs",
            )
        if self.archive_model_policy == CANONICAL_ARCHIVE_MODEL_POLICY:
            value.update(
                version="studio-source-page-budget/v7",
                archive_policy=self.archive_model_policy,
                working_state_reserve="already_counted_continuity_plus_prospective_refs",
                transcript="canonical_original_and_bounded_model_view/v1",
            )
        if self.archive_model_policy == OBSERVED_ARCHIVE_MODEL_POLICY:
            value.update(
                version="studio-source-page-budget/v8",
                archive_policy=self.archive_model_policy,
                working_state_reserve="already_counted_continuity_plus_prospective_refs",
                transcript="canonical_original_and_bounded_model_view/v2",
                summary="complete_observed_model_history/v1",
            )
        if self.message_projection_id is not None:
            value.update(version="studio-source-page-budget/v9",
                         message_projection=self.message_projection_id)
        if self.artifact_pages:
            value.update(version="studio-source-page-budget/v10",
                         source_roles=["workspace_read", "artifact_read"],
                         artifact_definition=digest(ARTIFACT_READ_DEFINITION),
                         artifact_page="preseal-contiguous-character-page/v1",
                         retained_summary_reserve="profile-output-ceiling/v1")
        if self.archive_model_policy == SOURCE_ARCHIVE_MODEL_POLICY:
            value.update(version="studio-source-page-budget/v11",
                         archive_policy=self.archive_model_policy,
                         archive_representation="captured-source-artifact/v1",
                         transcript="canonical_original_and_bounded_model_view/v2",
                         summary="complete_observed_model_history/v1")
        if self.artifact_result_max_bytes is not None:
            value.update(version="studio-source-page-budget/v14",
                         artifact_result_max_bytes=self.artifact_result_max_bytes,
                         artifact_page="preseal-contiguous-character-and-payload-page/v3",
                         page_feedback="preseal-active-capacity-reasons/v2",
                         prospective_refs="archive-producing-readers-only/v1")
        if self.context_planner is not None:
            value.update(version="studio-source-page-budget/v20",
                         artifact_inventory_projection=ARTIFACT_CATALOG_POLICY,
                         single_artifact_admission=SINGLE_ARTIFACT_CAPACITY_POLICY,
                         partial_capacity_projection=PARTIAL_ARTIFACT_CAPACITY_POLICY,
                         capacity_projection=ARTIFACT_CAPACITY_POLICY,
                         retained_summary_reserve="bound-planner-summary-and-framing/v1",
                         captured_workspace_admission="next-request-without-future-summary/v1",
                         no_summary_reader_projection=FINALIZATION_READER_PROJECTION_POLICY,
                         result_counter="complete-projected-message/v1",
                         context_tokenizer=self.context_planner.tokenizer_id,
                         context_policy=self.context_planner.policy.model_dump(mode="json"))
        return "sha256:" + hashlib.sha256(_json(value).encode()).hexdigest()

    def _declared_readers(self, snapshot, definitions):
        source_definitions = (
            (*definitions, ARTIFACT_READ_DEFINITION) if self.artifact_pages else definitions
        )
        names = {
            tool.name
            for tool in source_definitions
            if tool.read_only and (tool.context_role == "workspace_read" or (
                self.artifact_pages and tool.context_role == "artifact_read"
            ))
        }
        declared = next(
            (
                m
                for m in reversed(snapshot.messages)
                if m.get("role") == "assistant" and m.get("tool_calls")
            ),
            None,
        )
        calls = declared["tool_calls"] if declared else ()
        readers = [c for c in calls if c["function"]["name"] in names]
        return declared, readers

    def for_call(
        self, snapshot: RunSnapshot, call_id: str, definitions: tuple[ToolDefinition, ...]
    ):
        if snapshot.profile_digest != digest(self.profile):
            raise ValueError("source_read_profile_mismatch")
        context = snapshot.prepared_context
        if not context or not isinstance(context.get("instructions"), str):
            raise ValueError("source_read_context_unavailable")
        declared, readers = self._declared_readers(snapshot, definitions)
        if call_id not in {c["id"] for c in readers}:
            raise ValueError("source_read_batch_identity_invalid")
        messages = context.get("messages")
        counted = context.get("counted_input_tokens")
        if not isinstance(messages, (list, tuple)) or type(counted) is not int or counted < 1:
            raise ValueError("source_read_context_unavailable")
        message_counter = self.count_message_units or (lambda m: self.count_units(_json(m)))
        message_costs = [message_counter(message) for message in messages]
        if any(type(cost) is not int or cost < 1 for cost in message_costs):
            raise ValueError("source_read_counter_invalid")
        fixed = counted - sum(message_costs)
        if fixed < 1:
            raise ValueError("source_read_counter_invalid")
        # PreparedContext already measures the complete admitted model surface,
        # including Skill/MCP/subagent schemas, not just this builtin executor.
        mandatory = (
            fixed
            + (self.context_planner.count_model_message(
                {"role": "user", "content": snapshot.goal}, self.profile)
               if self.context_planner else self.count_units(
                   _json({"role": "user", "content": snapshot.goal})))
            + (self.context_planner.count_model_message(declared, self.profile)
               if self.context_planner else self.count_units(_json(declared)))
        )
        available = self.profile.context_window - self.profile.max_output_tokens - mandatory
        if self.artifact_only_fallback:
            # Newly minted refs also enter authoritative continuity on the
            # next request. Charge their maximum declared locator surface
            # before dividing this batch; this is capacity, not a future fact.
            producers = readers
            # A byte-bound reserved ArtifactRead cannot create a new reference:
            # its complete result fits the same host RunLimits ceiling and its
            # locked definition has no forced archive policy. Existing refs
            # remain charged in the prepared fixed surface above. Unbound or
            # non-reserved definitions keep the conservative original reserve.
            installed = [tool for tool in definitions
                         if tool.name == ARTIFACT_READ_DEFINITION.name]
            if (
                self.artifact_result_max_bytes is not None and len(installed) == 1
                and installed[0].model_copy(update={"feedback_profile_digest": None})
                == ARTIFACT_READ_DEFINITION
            ):
                producers = [c for c in readers
                             if c["function"]["name"] != ARTIFACT_READ_DEFINITION.name]
            available -= self.count_units(
                _json(
                    {
                        "artifact_refs": [
                            "artifact://" + "a" * (ARCHIVE_URI_MAX_CHARS - 11) for _ in producers
                        ]
                    }
                )
            ) if producers else 0
        # Reserve half for result envelopes and subsequent task observations.
        # The source counter governs returned text, not provider usage or a
        # claim that the entire subsequent request is measured here.
        allowance = available // 2 // len(readers)
        # Leave one profile output ceiling for the rolling summary and new
        # authoritative state. Bound each complete result separately; the
        # source share alone does not cover escaped JSON or pagination metadata.
        # Older history may still require normal compaction.
        outer = self.count_units(_json({"role": "tool", "tool_call_id": call_id, "content": ""}))
        # v6's fixed cost already includes the current complete continuity,
        # and available already reserves the next output and prospective refs.
        # Legacy policies retain their original rolling-summary reserve.
        # v10 additionally admits the latest page group as a retained working
        # set beside a new summary. That summary is future input, distinct
        # from the next task response's output ceiling already charged above.
        rolling_reserve = (
            0 if not self.artifact_pages and self.archive_model_policy in {
                ARCHIVE_SEARCH_MODEL_POLICY, CANONICAL_ARCHIVE_MODEL_POLICY,
                OBSERVED_ARCHIVE_MODEL_POLICY,
            }
            else self.profile.max_output_tokens
        )
        if self.context_planner is not None:
            rolling_reserve = self.context_planner.summary_reservation(self.profile)
            # A captured workspace result may itself be summarized when the
            # complete subsequent request needs compaction. Unlike a quoted
            # ArtifactRead working set, it does not require a simultaneous
            # future summary beside that result. Reserving both here can
            # reject even the saved-unread envelope while the real next
            # request still fits. Keep output, prospective artifact locators
            # and complete result framing charged; ContextPlanner remains the
            # hard authority and must preserve/quote the observation on overflow.
            workspace_names = {tool.name for tool in definitions
                               if tool.read_only and tool.context_role == "workspace_read"}
            if (self.retain_requested_source and self.artifact_only_fallback
                    and all(c["function"]["name"] in workspace_names for c in readers)):
                rolling_reserve = 0
        result_allowance = (available - rolling_reserve) // len(readers) - outer
        return SourceReadBudget(
            allowance,
            self.count_units,
            self.counter_id,
            result_allowance,
            WORKSPACE_SOURCE_MODEL_VERSION,
            retain_requested_source=self.retain_requested_source,
            artifact_only_fallback=self.artifact_only_fallback,
            archive_model_policy=self.archive_model_policy,
        )

    def for_artifact_call(self, snapshot, call_id, definitions):
        if not self.artifact_pages:
            raise ValueError("source_read_artifact_policy_unavailable")
        _, readers = self._declared_readers(snapshot, definitions)
        group_size = len(readers) if self.artifact_result_max_bytes is not None else None
        try:
            budget = self.for_call(snapshot, call_id, definitions)
        except ValueError as exc:
            if str(exc) == "source_read_budget_unavailable":
                raise ArtifactPageCapacityError(
                    "artifact_page_budget_unavailable", source_group_size=group_size,
                ) from exc
            raise
        return ArtifactPageBudget(
            budget.max_units, budget.max_result_units, self.count_units,
            max_payload_bytes=self.artifact_result_max_bytes,
            source_group_size=group_size,
        )

    def artifact_policy(self, store, *, workspace_revision, workspace_tracking):
        """Delay binding until installation has assembled this Run's final tools.

        Each parent/child composition owns its immutable configuration closure;
        no mutable registry or parent task contract is shared across Runs.
        """
        def unbound(_request, _definitions):
            raise ValueError("source_read_configuration_unbound")

        def bind(configuration):
            configuration = RunConfiguration.model_validate_json(configuration.model_dump_json())
            driver = ContextDriver(self.context_planner, workspace_revision,
                                   workspace_tracking=workspace_tracking)

            def resolve(request, definitions):
                snapshot = store.load(request.scope, request.run_id)
                if (configuration.profile != self.profile
                        or configuration.limits.max_tool_result_bytes
                        != self.artifact_result_max_bytes
                        or configuration.tools != definitions):
                    raise ValueError("source_read_configuration_mismatch")
                quote = artifact_group_capacity(snapshot, configuration, driver)
                if quote is None:
                    admission = partial_artifact_group_admission(snapshot, configuration, driver)
                    budget = self.for_artifact_call(snapshot, request.call.call_id, definitions)
                    if admission is None:
                        return budget
                    if (request.scope != snapshot.scope or request.run_id != snapshot.run_id
                            or request.operation_key != snapshot.pending_intent
                            or request.call.model_dump(mode="json") != snapshot.pending_tools[0]):
                        raise ValueError("source_read_batch_identity_invalid")
                    return replace(budget, admit_result=admission)
                call_id = request.call.call_id
                if call_id not in {c["call_id"] for c in snapshot.pending_tools}:
                    raise ValueError("source_read_batch_identity_invalid")

                def count_result(page):
                    return self.context_planner.count_model_message({
                        "role": "tool", "tool_call_id": call_id,
                        "content": json.dumps({"status": "ok", "content": page},
                                              ensure_ascii=False),
                    }, self.profile)

                budget = ArtifactPageBudget(
                    quote.source_units, quote.result_message_units, self.count_units,
                    max_payload_bytes=self.artifact_result_max_bytes,
                    source_group_size=quote.group_size, count_result_message=count_result,
                )
                if (quote.group_size == 1
                        and getattr(request, "scope", None) == snapshot.scope
                        and getattr(request, "run_id", None) == snapshot.run_id
                        and getattr(request, "operation_key", None) == snapshot.pending_intent
                        and request.call.model_dump(mode="json") == snapshot.pending_tools[0]):
                    admission = single_artifact_group_admission(snapshot, configuration, driver)
                    if admission is not None:
                        # Keep the estimate as the fast path. A complete page
                        # can alternatively enter a fully covered summary;
                        # real planning then admits all unchanged framing.
                        return replace(
                            budget, max_result_units=budget.max_result_units + quote.summary_tokens,
                            admit_result=lambda result: (
                                budget.fits(result.content) or admission(result)
                            ),
                        )
                return budget

            return ArtifactPagePolicy(self.digest, resolve)

        return ArtifactPagePolicy(self.digest, unbound, bind_configuration=bind)


    def artifact_search_policy(self, store, *, workspace_revision, workspace_tracking):
        """Bind Search output capacity independently for this assembled Run."""
        from ksadk.harness.artifact_adapters.native_search_budget import (
            ArtifactSearchBudget,
            ArtifactSearchPolicy,
        )
        from ksadk.harness.artifact_adapters.native_search_budget import (
            digest as search_digest,
        )
        from ksadk.harness.native.artifact_search_capacity import (
            SEARCH_CAPACITY_POLICY,
            search_group_capacity,
        )
        identity = search_digest({"policy": SEARCH_CAPACITY_POLICY, "source_policy": self.digest})
        def unbound(_request, _definitions):
            raise ValueError("artifact_search_configuration_unbound")
        def bind(configuration):
            configuration = RunConfiguration.model_validate_json(configuration.model_dump_json())
            driver = ContextDriver(self.context_planner, workspace_revision,
                                   workspace_tracking=workspace_tracking)
            def resolve(request, definitions):
                snapshot = store.load(request.scope, request.run_id)
                if (configuration.profile != self.profile
                        or configuration.limits.max_tool_result_bytes
                        != self.artifact_result_max_bytes
                        or configuration.tools != definitions
                        or not source_request_identity(snapshot, request, configuration)):
                    raise ValueError("artifact_search_configuration_mismatch")
                quote = search_group_capacity(snapshot, configuration, driver)
                if quote is None:
                    raise ValueError("artifact_search_capacity_unavailable")
                def count_result(result):
                    return self.context_planner.count_model_message({
                        "role": "tool", "tool_call_id": request.call.call_id,
                        "content": json.dumps({"status": result.status, "content": result.content},
                                              ensure_ascii=False),
                    }, self.profile)
                return ArtifactSearchBudget(quote.result_message_units, count_result,
                                            self.artifact_result_max_bytes)
            return ArtifactSearchPolicy(identity, resolve)
        return ArtifactSearchPolicy(identity, unbound, bind_configuration=bind)

#!/usr/bin/env python3
"""Prepare a clean ksadk-python export candidate.

This helper is intentionally local-only. It never creates a GitHub repository,
pushes branches, enables Pages, creates releases, or publishes packages. It
exists so maintainers can review a deterministic clean-export directory before
deciding whether the public GitHub import should use rewritten history or a
fresh source snapshot.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
TARGET_REPOSITORY = "https://github.com/kingsoftcloud/ksadk-python"
DOCUMENTATION_URL = "https://kingsoftcloud.github.io/ksadk-python/"

DEFAULT_OUTPUT_DIR = Path("/tmp/ksadk-python-export-candidate")

CURATED_DOCS: set[str] = {
    "docs/adk-multi-version-compat.md",
    "docs/maintainer-approval-record.md",
    "docs/public-release-workflow.md",
}
CURATED_REFERENCE_DOCS: set[str] = {"docs/reference/ksadk环境变量参考.md"}

ROOT_EXPORT_FILES = {
    # Preserve already-reviewed public main inputs in subsequent clean exports.
    ".github/actions/prepare-web-candidate/action.yml",
    ".dockerignore",
    ".gitattributes",
    ".gitleaks.toml",
    ".github/BRANCH_PROTECTION.md",
    ".github/ISSUE_TEMPLATE/bug_report.md",
    ".github/ISSUE_TEMPLATE/feature_request.md",
    ".github/dependabot.yml",
    ".github/pull_request_template.md",
    ".github/codeql/codeql-config.yml",
    ".github/workflows/ci.yml",
    ".github/workflows/codeql.yml",
    ".github/workflows/pages.yml",
    ".github/workflows/publish-pypi.yml",
    ".github/workflows/release-check.yml",
    ".github/workflows/secret-patterns.yml",
    ".gitignore",
    "CHANGELOG.md",
    "CONTRIBUTING.md",
    "AGENTS.md",
    "CLAUDE.md",
    "LICENSE",
    "MANIFEST.in",
    "Makefile",
    "README.md",
    "README.en.md",
    "README.zh-CN.md",
    "SECURITY.md",
    "pyproject.toml",
    "uv.lock",
}

EXPORT_PREFIXES = (
    "contracts/",
    "docs-site/",
    "ksadk/",
    "ksadk_runtime_common/",
)

# Public Git trees carry build inputs; static assets are generated for packages.
REQUIRED_PUBLIC_FILES = {
    "AGENTS.md",
    "CLAUDE.md",
    "LICENSE",
    "README.md",
    "README.en.md",
    "README.zh-CN.md",
    ".gitleaks.toml",
    "docs-site/package.json",
    "docs-site/pnpm-lock.yaml",
    "ksadk/studio/react-ui/package.json",
    "ksadk/studio/react-ui/package-lock.json",
    "ksadk/studio/react-ui/vendor/ksadk-web-source.json",
    "pyproject.toml",
}

SCRIPT_EXPORT_FILES = {
    # Preserve already-reviewed public main inputs in subsequent clean exports.
    'scripts/export_teams_cloud_contracts.py',
    'scripts/generate_teams_completion_fixtures.py',
    'scripts/generate_teams_extended_fixtures.py',

    "scripts/audit_docs_site_output.py",
    "scripts/audit_release_artifacts.py",
    "scripts/build_alias_distribution.py",
    "scripts/check_approval_record.py",
    "scripts/check_publication_state.py",
    "scripts/check_release_version.py",
    "scripts/generate_public_assets.py",
    "scripts/open_source_audit.py",
    "scripts/release_candidate_gate.py",
    "scripts/release_preflight.py",
    "scripts/prepare_ksadk_python_export.py",
    "scripts/prepare_ksadk_web_export.py",
    "scripts/public_secret_audit.py",
    "scripts/verify_ksadk_web_static.py",
    "scripts/reviewed_web_assets.py",
    "scripts/write_build_provenance.py",
    "scripts/verify_native_studio_install.py",
}

PUBLIC_TEST_FILES = {
    # Keep the regressions uncovered by the full internal alignment suite.
    "tests/cli/test_web_managed_checkpoint_env.py",
    "tests/test_local_event_sequence_concurrency.py",
    "tests/test_sessions_service.py",
    "tests/test_stm_config.py",
    "tests/studio/native_control_fixture.py",
    "tests/studio/test_native_browser_port.py",
    "tests/studio/test_native_delivery_evidence_routing.py",
    "tests/studio/test_native_final_shape.py",
    "tests/studio/test_native_product_child_approval.py",
    "tests/studio/test_native_product_child_deadline.py",
    "tests/studio/test_native_product_durability.py",
    "tests/studio/test_native_product_edit_revision.py",
    "tests/studio/test_native_workspace_diagnostics.py",
    "tests/studio/test_run_service.py",
    "tests/studio/test_runtime_prewarm.py",
    "tests/studio/test_scheduler_dsh_harness_product.py",
    "tests/studio/test_scheduler_history.py",
    "tests/harness/native/lsp_fixture.py",
    "tests/harness/native/test_language_diagnostics.py",
    "tests/postgres_isolation.py",
    "tests/resource_runtime/test_discovery_worker.py",
    "tests/resource_runtime/test_skill_packages.py",
    "tests/resource_runtime/test_skill_client_isolation.py",
    "tests/skills/test_service_client_http.py",
    "tests/skills/test_request_kop_signing.py",
    "tests/harness/native/__init__.py",
    "tests/test_public_secret_audit.py",
    "tests/packaging/test_reviewed_web_assets.py",
    # Keep reviewed public regressions and the CI test dependency closure.
    "tests/a2a/fixtures/remote_agent/v1/a2a_stream_tool_terminal.jsonl",
    "tests/a2a/test_invocation_service_r85.py",
    "tests/a2a/test_reasoning_streaming.py",
    "tests/cli/test_hosted_invoke.py",
    "tests/cli/test_hosted_invoke_session_route.py",
    "tests/codex/conftest.py",
    "tests/codex/test_proxy_injection.py",
    "tests/events/test_managed_scope_reducer.py",
    "tests/harness/conftest.py",
    "tests/harness/native/compaction_reserve_process_fixture.py",
    "tests/harness/native/conftest.py",
    "tests/harness/native/partial_summary_process_fixture.py",
    "tests/harness/native/summary_recovery_process_fixture.py",
    "tests/harness/native/test_artifact_continuity.py",
    "tests/harness/native/test_captured_source_artifact.py",
    "tests/harness/native/test_compaction_finalization_reserve.py",
    "tests/harness/native/test_compaction_reserve_process_recovery.py",
    "tests/harness/native/test_context_runtime.py",
    "tests/harness/native/test_context_summary_recovery.py",
    "tests/harness/native/test_effect_cleanup_cancellation.py",
    "tests/harness/native/test_kernel.py",
    "tests/harness/native/test_native_runtime.py",
    "tests/harness/native/test_parallel_member_history.py",
    "tests/harness/native/test_partial_summary_process.py",
    "tests/harness/native/test_run_wall_budget.py",
    "tests/harness/native/test_summary_recovery_process.py",
    "tests/interaction/test_ledger_conformance.py",
    "tests/kernel/store_conformance.py",
    "tests/kernel/test_orphan_sessions.py",
    "tests/kernel/test_production_bootstrap.py",
    "tests/kernel/test_scheduler_metadata_postgres.py",
    "tests/kernel/test_session_quarantine.py",
    "tests/kernel/test_worker.py",
    "tests/packaging/test_alias_distribution.py",
    "tests/fixtures/adk_two_llm_outputs.json",
    "tests/runners/test_adk_multi_llm_runtime_events.py",
    "tests/runtime/test_gateway_approval_history.py",
    "tests/studio/native_recovery_lease_fixture.py",
    "tests/studio/test_dsh_provider_registration.py",
    "tests/studio/test_model_chat_snapshots.py",
    "tests/studio/test_model_chat_tools.py",
    "tests/studio/test_model_client.py",
    "tests/studio/test_model_empty_output_limit.py",
    "tests/studio/test_model_incomplete_finish_reason.py",
    "tests/studio/test_model_wire_identity.py",
    "tests/studio/test_model_wire_usage.py",
    "tests/studio/test_native_artifact_context_capacity.py",
    "tests/cli/test_cmd_config_tunnel.py",
    "tests/studio/test_native_default_install.py",
    "tests/studio/test_native_model_port.py",
    "tests/test_canonical_write_budget_postgres.py",
    "tests/test_cloud_headers.py",
    "tests/test_code_builder_build_info.py",
    "tests/test_framework_db_budget_postgres.py",
    "tests/test_history_cache_postgres.py",
    "tests/test_mainline_history_merge_postgres.py",
    "tests/test_mcp_oauth_extension.py",
    "tests/test_mcp_oauth_recovery.py",
    "tests/test_pool_release_budget_postgres.py",
    "tests/test_prepare_ksadk_python_export.py",
    "tests/test_resilient_unknown_commit.py",
    "tests/test_runtime_db_history_contract.py",
    "tests/test_subscription_reads_postgres.py",

    # Preserve already-reviewed public main inputs in subsequent clean exports.
    'tests/contracts/fixtures/teams-cloud-v1.json',
    'tests/contracts/test_teams_cloud_contracts.py',
    'tests/contracts/test_teams_cloud_permits.py',
    'tests/events/test_canonical_store_subscription.py',
    'tests/harness/test_execution_policy.py',
    'tests/kernel/control_harness.py',
    'tests/kernel/loaded_build_harness.py',
    'tests/kernel/teams_host_harness.py',
    'tests/kernel/test_postgres_locks.py',
    'tests/kernel/test_contracts.py',
    'tests/kernel/test_execution_grants.py',
    'tests/kernel/test_teams_artifact_tools.py',
    'tests/kernel/test_teams_effects.py',
    'tests/kernel/test_teams_execution_context.py',
    'tests/kernel/test_teams_host_http.py',
    'tests/kernel/test_teams_host_ingress.py',
    'tests/kernel/test_teams_material_transport.py',
    'tests/kernel/test_teams_materials.py',
    'tests/kernel/test_teams_runtime_bootstrap.py',
    'tests/kernel/test_teams_sqlite_concurrency.py',
    'tests/kernel/test_teams_tool_runtime.py',
    'tests/kernel/test_teams_workspace.py',
    'tests/plugins/test_teams_build_artifacts.py',
    'tests/plugins/test_teams_execution_completion.py',
    'tests/plugins/test_teams_runtime.py',
    'tests/server/test_approval_terminal_resume.py',
    'tests/server/test_checkpoint_resume_attempt_audit.py',
    'tests/server/test_checkpoint_resume_audit.py',
    'tests/server/test_historical_agent_approval_and_blocks.py',
    'tests/server/test_postgres_jsonb_codec_guard.py',
    'tests/server/test_resume_run_idempotency.py',
    'tests/server/test_tool_receipt_claim_routes.py',
    'tests/studio/node_v1_crash_fixture.py',
    'tests/studio/test_execution_host.py',
    'tests/studio/test_teams_node_factory.py',
    'tests/studio/test_teams_node_v1.py',
    'tests/test_runtime_persistence_resume_idempotency.py',
    'tests/test_runtime_resume_receipt_idempotency.py',
    'tests/test_runtime_resume_side_effect_ledger.py',
    'tests/test_tool_receipt_claim.py',
    'tests/test_tool_receipt_claim_concurrency.py',

    "tests/plugins/test_teams_migration.py",
    "tests/plugins/test_teams_postgres.py",
    "tests/memory/test_postgres_provider_live.py",
    "tests/plugins/test_teams_workspaces.py",
    "tests/studio/e2e/studio_creation_layout_smoke.py",
    "tests/studio/test_dsh_studio_assets.py",
    "tests/studio/test_teams_catalog.py",
    "tests/studio/test_teams_installation.py",
    "tests/studio/test_teams_node.py",
    "tests/studio/test_teams_remote.py",
    "tests/studio/e2e/studio_style_comparison.py",
    "tests/studio/test_scheduler_runtime.py",
    "tests/test_check_release_version.py",
    "tests/studio/test_shared_web.py",
    "tests/studio/test_dsh_application.py",
    "tests/__init__.py",
    "tests/conftest.py",
    "tests/events/fixtures/runtime_projection_golden.json",
    "tests/compat/fixtures/v0.8.2-agent-bundle.provenance.json",
    "tests/compat/fixtures/v0.8.2-agent-bundle.zip.b64",
    "tests/compat/fixtures/v0.8.2-managed-runtime-agentengine.provenance.json",
    "tests/compat/fixtures/v0.8.2-managed-runtime-agentengine.yaml",
    "tests/compat/test_release_legacy_compat.py",
    "tests/compat/test_release_082_asset_compat.py",
    "tests/e2e/test_codex_plugin_bridge_e2e.py",
    "tests/e2e/test_codex_provider_app_server_e2e.py",
    "tests/e2e/test_codex_subagent_provider_e2e.py",
    "tests/e2e/fixtures/codex-marketplace/.agents/plugins/marketplace.json",
    "tests/e2e/fixtures/codex-marketplace/plugins/ksadk-bridge-e2e/.codex-plugin/plugin.json",
    "tests/e2e/fixtures/codex-marketplace/plugins/ksadk-bridge-e2e/.mcp.json",
    "tests/e2e/fixtures/codex-marketplace/plugins/ksadk-bridge-e2e/scripts/fixture_mcp.py",
    "tests/e2e/fixtures/codex-marketplace/plugins/ksadk-bridge-e2e/skills/bridge-audit/SKILL.md",
    "tests/e2e/fixtures/codex-marketplace/plugins/ksadk-bridge-e2e/skills/bridge-check/SKILL.md",
    "tests/e2e/test_dsh_managed_toolchain_e2e.py",
    "tests/e2e/chat_completions_stub.py",
    "tests/e2e/codex_app_server_fixture.py",
    "tests/fixtures/dsh-capability-fake-profile.mjs",
    "tests/fixtures/dsh-node-tool-plugin/cordis.patch.yml",
    "tests/fixtures/dsh-node-tool-plugin/index.mjs",
    "tests/fixtures/dsh-node-tool-plugin/package.json",
    "tests/fixtures/dsh-node-agent-provider/cordis.patch.yml",
    "tests/fixtures/dsh-node-agent-provider/index.mjs",
    "tests/fixtures/dsh-node-agent-provider/package.json",
    "tests/fixtures/dsh-node-agent-provider/provider-host.mjs",
    "tests/e2e/codex_responses_stub.py",
    "tests/harness/__init__.py",
    "tests/harness/fixtures/__init__.py",
    "tests/harness/fixtures/mcp_server.py",
    "tests/packaging/test_release_candidate_gate.py",
    "tests/packaging/test_release_preflight.py",
    "tests/packaging/test_write_build_provenance.py",
    "tests/packaging/test_teams_distribution.py",
    "tests/plugins/__init__.py",
    "tests/plugins/test_codex_manifest.py",
    "tests/plugins/test_codex_plugin_bridge.py",
    "tests/plugins/test_codex_stdio_mcp.py",
    "tests/plugins/test_plugin_lock_upstream.py",
    "tests/plugins/test_dsh_node_provider_e2e.py",
    "tests/plugins/test_dsh_capability_host.py",
    "tests/plugins/test_dsh_capability_host_e2e.py",
    "tests/plugins/test_dsh_upstream_plugin_e2e.py",
    "tests/plugins/test_dsh_source_policy.py",
    "tests/plugins/test_codex_provider_vertical.py",
    "tests/test_check_approval_record.py",
    "tests/test_check_publication_state.py",
    "tests/test_config_env_registry.py",
    "tests/test_deploy_env_forward.py",
    "tests/test_docs_site_output_audit.py",
    "tests/test_markdown_repair.py",
    "tests/test_open_source_audit.py",
    "tests/test_public_release_positioning.py",
    "tests/test_runtime_common_packaging.py",
    "tests/studio/test_style_system.py",
    "tests/studio/__init__.py",
    "tests/studio/test_framework_bundle_integrity.py",
    "tests/studio/test_codex_plugin_store.py",
    "tests/studio/test_dsh_capability_service.py",
    "tests/studio/test_resource_authority.py",
    "tests/resource_runtime/test_activation_approvals.py",
    "tests/resource_runtime/test_memory_worker.py",
    "tests/resource_runtime/test_worker_process.py",
    "tests/resource_runtime/test_studio_resource_config.py",
    "tests/fixtures/resource_runtime/node_bridge.test.mjs",
    "tests/fixtures/resource_runtime/node_bridge_probe.mjs",
    "tests/fixtures/resource_runtime/node_core_probe.mjs",
    "tests/fixtures/resource_runtime/node_mcp_host.mjs",
    "tests/fixtures/resource_runtime/plain_status_plugin.mjs",
    "tests/studio/test_dsh_agent_binding.py",
    "tests/studio/test_dsh_plugin_api.py",
    "tests/studio/test_native_plugin_binding.py",
    "tests/studio/runtime_adapter_fixtures.py",
    "tests/studio/e2e/conversation_items_browser_e2e.py",
    "tests/studio/e2e/conversation_reconnect_browser_e2e.py",
    "tests/studio/e2e/scheduler_browser_e2e.py",
    "tests/studio/e2e/scheduler_fault_matrix_browser_e2e.py",
    "tests/studio/e2e/scheduler_harness_browser_e2e.py",
    "tests/studio/e2e/studio_browser_smoke.py",
    "tests/studio/e2e/studio_workspace_navigation_smoke.py",
    "tests/studio/e2e/studio_layout_smoke.py",
    "tests/studio/e2e/studio_resource_forms_smoke.py",
    "tests/studio/e2e/studio_workflow_layout_smoke.py",
    "tests/studio/e2e/studio_e2e_support.py",
    "tests/studio/e2e/studio_responsive_smoke.py",
    "tests/test_managed_runtime_builder.py",
    "tests/test_managed_runtime_native_smoke.py",
    "tests/test_managed_runtime_resolution.py",
    "tests/test_tracing_setup_otlp.py",
    "tests/cli/test_cmd_create_codex.py",
    "tests/runners/test_adapter_contract.py",
    "tests/runners/test_codex_plugin_bootstrap.py",
    "tests/runners/test_codex_runner.py",
    "tests/runners/test_hitl_resume_envelopes.py",
    "tests/runtime/test_semantic_tool_scope.py",
    "tests/test_runner.py",
    "tests/test_langgraph_runner_resume.py",
    "tests/runners/test_langchain_hitl_langgraph.py",
    "tests/runners/test_langgraph_session_identity.py",
    "tests/architecture/test_python_module_size.py",
    "tests/context_engine/test_capabilities.py",
    "tests/studio/test_e2e_server_readiness.py",
    "tests/test_resilient_canonical_authority.py",
    "tests/a2a/test_public_imports.py",
    "tests/studio/test_shared_web_final_snapshot.py",
    "tests/studio/response_projection_fixtures.py",
    # On-prem MCP Tunnel runtime, protocol, and local end-to-end coverage.
    "tests/tunnel/__init__.py",
    "tests/tunnel/conftest.py",
    "tests/tunnel/test_connector.py",
    "tests/tunnel/test_langgraph.py",
    "tests/tunnel/test_registry.py",
    "tests/tunnel/test_server.py",
    "tests/tunnel/test_toolset.py",
    "tests/test_tunnel_runtime.py",
    "tests/test_tunnel_e2e.py",
}

# Reviewed public protocol regressions and their complete local dependency closure.
PUBLIC_PROTOCOL_TEST_FILES = {
    'tests/events/adapters/test_adk.py',
    'tests/events/test_stream_reducer.py',
    'tests/events/test_runtime_identity.py',
    'tests/codex/fake_app_server.py',
    'tests/codex/test_managed_process_recovery.py',
    'tests/codex/managed_recovery_process.py',
    'tests/codex/test_managed_checkpoint_real.py',
    'tests/a2a/compose.yaml',
    'tests/a2a/fixtures/README.md',
    'tests/a2a/fixtures/a2a-langgraph-a/a2a_langgraph_a/agent.py',
    'tests/a2a/fixtures/a2a-langgraph-b/a2a_langgraph_b/agent.py',
    'tests/a2a/fixtures/a2a-langgraph-b/a2a_langgraph_b/fault_probe.py',
    'tests/a2a/fixtures/a2a-langgraph-c/a2a_langgraph_c/agent.py',
    'tests/a2a/fixtures/a2a-langgraph-c/a2a_langgraph_c/mcp_server.py',
    'tests/a2a/fixtures/a2a-r86-adk/a2a_r86_adk/agent.py',
    'tests/a2a/fixtures/remote_agent/v1/a2a_input_continue_cancel.jsonl',
    'tests/a2a/fixtures/remote_agent/v1/a2a_stream_tool_terminal.jsonl',
    'tests/a2a/fixtures/remote_agent/v1/adk_two_llm_outputs.jsonl',
    'tests/a2a/fixtures/remote_agent/v1/expected_conversation_projection.json',
    'tests/a2a/fixtures/remote_agent/v1/expected_runtime_projection.json',
    'tests/a2a/fixtures/remote_agent/v1/expected_v1_projection.json',
    'tests/a2a/fixtures/remote_agent/v1/manifest.json',
    'tests/a2a/kernel_native_process_worker.py',
    'tests/a2a/langgraph_kernel_process_worker.py',
    'tests/a2a/langgraph_process_recovery_worker.py',
    'tests/a2a/native_process_recovery_worker.py',
    'tests/a2a/network_native_caller.py',
    'tests/a2a/pg_process_worker.py',
    'tests/a2a/process_server.py',
    'tests/a2a/product_kernel_execution_worker.py',
    'tests/a2a/product_kernel_fact_reader.py',
    'tests/a2a/product_official_client_worker.py',
    'tests/a2a/product_protocol_roundtrip.py',
    'tests/a2a/selected_kernel_database_worker.py',
    'tests/a2a/tck_protocol_worker.py',
    'tests/a2a/test_a2a_dependency_contract.py',
    'tests/a2a/test_a2a_discovery.py',
    'tests/a2a/test_a2a_protocol_e2e.py',
    'tests/a2a/test_adk_interaction_session_isolation.py',
    'tests/a2a/test_adk_invocation_context.py',
    'tests/a2a/test_adk_managed_tools.py',
    'tests/a2a/test_adk_runtime.py',
    'tests/a2a/test_build_exposure_validation.py',
    'tests/a2a/test_card.py',
    'tests/a2a/test_codex_expert.py',
    'tests/a2a/test_context_adapter.py',
    'tests/a2a/test_context_store.py',
    'tests/a2a/test_control_plane.py',
    'tests/a2a/test_credential_and_outbound.py',
    'tests/a2a/test_executor_cancel_honesty.py',
    'tests/a2a/test_executor_output_identity.py',
    'tests/a2a/test_executor_resume_runtime_adapter.py',
    'tests/a2a/test_exposure.py',
    'tests/a2a/test_external_transport.py',
    'tests/a2a/test_inbound_admission_recovery_postgres.py',
    'tests/a2a/test_inbound_kernel.py',
    'tests/a2a/test_invocation_checkpoint.py',
    'tests/a2a/test_invocation_result_recovery.py',
    'tests/a2a/test_invocation_service_r85.py',
    'tests/a2a/test_invocation_service_recovery.py',
    'tests/a2a/test_invocation_types.py',
    'tests/a2a/test_kernel_native_process_recovery.py',
    'tests/a2a/test_kernel_task_adapter.py',
    'tests/a2a/test_kernel_terminal_reconciliation.py',
    'tests/a2a/test_langgraph_gateway_compat.py',
    'tests/a2a/test_langgraph_invocation_context.py',
    'tests/a2a/test_langgraph_kernel_approval.py',
    'tests/a2a/test_langgraph_kernel_process_recovery.py',
    'tests/a2a/test_langgraph_managed.py',
    'tests/a2a/test_langgraph_process_recovery.py',
    'tests/a2a/test_langgraph_tool_runtime.py',
    'tests/a2a/test_local_managed_network.py',
    'tests/a2a/test_managed_a2a_card.py',
    'tests/a2a/test_managed_bootstrap.py',
    'tests/a2a/test_managed_checkpoint_ready_postgres.py',
    'tests/a2a/test_managed_entrypoint_composition.py',
    'tests/a2a/test_managed_expert_timeout_cleanup.py',
    'tests/a2a/test_managed_invocation_budget.py',
    'tests/a2a/test_managed_space_client_selection.py',
    'tests/a2a/test_managed_timeout_config.py',
    'tests/a2a/test_native_process_recovery.py',
    'tests/a2a/test_native_tool_http_roundtrip.py',
    'tests/a2a/test_owner_resolver.py',
    'tests/a2a/test_pg_managed_stores.py',
    'tests/a2a/test_pg_owned_outbox.py',
    'tests/a2a/test_pg_process_recovery.py',
    'tests/a2a/test_platform_composition.py',
    'tests/a2a/test_policy_refs.py',
    'tests/a2a/test_presentation_review.py',
    'tests/a2a/test_process_interop.py',
    'tests/a2a/test_product_control_cross_repo.py',
    'tests/a2a/test_product_forward_cross_repo.py',
    'tests/a2a/test_product_kernel_facts.py',
    'tests/a2a/test_product_kernel_server_roundtrip.py',
    'tests/a2a/test_product_key_client.py',
    'tests/a2a/test_public_imports.py',
    'tests/a2a/test_public_tool_http_roundtrip.py',
    'tests/a2a/test_public_tool_producer_r85.py',
    'tests/a2a/test_r86_examples.py',
    'tests/a2a/test_reasoning_streaming.py',
    'tests/a2a/test_remote_agent_fixture.py',
    'tests/a2a/test_remote_approval_rejection.py',
    'tests/a2a/test_result_text.py',
    'tests/a2a/test_resume_store.py',
    'tests/a2a/test_runtime_approval.py',
    'tests/a2a/test_selected_kernel_database.py',
    'tests/a2a/test_session_followup_history_regression.py',
    'tests/a2a/test_split_examples.py',
    'tests/a2a/test_task_event_dispatcher.py',
    'tests/a2a/test_terminal_protocol_errors.py',
    'tests/a2a/test_tool_outcome_guard.py',
    'tests/a2a/test_trusted_identity.py',
    'tests/a2a/test_workload_token_public_compat.py',
    'tests/a2ui/test_a2ui_core.py',
    'tests/a2ui/test_a2ui_renderer.py',
    'tests/a2ui/test_catalog_input_bounds.py',
    'tests/a2ui/test_custom_catalog.py',
    'tests/a2ui/test_native_surface_stream.py',
    'tests/a2ui/test_official_wire.py',
    'tests/a2ui/test_presentation_builder.py',
    'tests/a2ui/test_registered_generation.py',
    'tests/agui/__init__.py',
    'tests/agui/test_a2ui_generation_visibility.py',
    'tests/agui/test_copilotkit_a2ui_compat.py',
    'tests/agui/test_default_local_interactions.py',
    'tests/agui/test_foreground_local_interactions.py',
    'tests/agui/test_interaction_rejection_subscription.py',
    'tests/agui/test_kernel_native_input.py',
    'tests/agui/test_langgraph_interrupt_e2e.py',
    'tests/agui/test_langgraph_timeout_recovery.py',
    'tests/agui/test_native_cold_resume.py',
    'tests/agui/test_native_shutdown_recovery.py',
    'tests/agui/test_native_typed_resume.py',
    'tests/agui/test_protocol_contract.py',
    'tests/agui/test_runtime_adapter.py',
    'tests/agui/test_runtime_preprocessing.py',
    'tests/agui/test_streamed_tool_projection.py',
    'tests/agui/test_terminal_outcomes.py',
    'tests/agui/test_text_snapshot_projection.py',
    'tests/builders/test_managed_a2a_entrypoint.py',
    'tests/cli/test_a2a_register_hosted.py',
    'tests/cli/test_cmd_a2a_runtime_adapter.py',
    'tests/codex/a2a_app_server.py',
    'tests/codex/conftest.py',
    'tests/codex/test_a2a_bridge.py',
    'tests/codex/test_a2a_factory.py',
    'tests/codex/test_a2a_real_app_server.py',
    'tests/codex/test_a2a_runtime.py',
    'tests/codex/test_sdk_transport.py',
    'tests/conftest.py',
    'tests/conversations/test_native_a2ui_projection.py',
    'tests/e2e/a2ui_mcp_server.py',
    'tests/e2e/a2ui_runtime_app.py',
    'tests/e2e/codex_app_server_fixture.py',
    'tests/e2e/codex_responses_stub.py',
    'tests/events/adapters/test_a2a.py',
    'tests/events/adapters/test_a2a_checkpoint.py',
    'tests/events/test_managed_scope_reducer.py',
    'tests/fixtures/a2ui/custom_catalog.json',
    'tests/fixtures/local_oauth_provider.py',
    'tests/interaction/test_a2ui_native_approval.py',
    'tests/interaction/test_langgraph_upgrade_semantics.py',
    'tests/interaction/test_ledger_conformance.py',
    'tests/interaction/test_legacy_a2ui_surface_binding.py',
    'tests/interaction/test_native_confirmation_pipeline.py',
    'tests/kernel/control_harness.py',
    'tests/kernel/store_conformance.py',
    'tests/kernel/test_a2a_bounded_reads_postgres.py',
    'tests/kernel/test_a2a_observation_recovery_postgres.py',
    'tests/kernel/test_postgres_store.py',
    'tests/kernel/test_production_bootstrap.py',
    'tests/kernel/test_recovery.py',
    'tests/kernel/test_worker.py',
    'tests/plugins/test_codex_provider_vertical.py',
    'tests/postgres_isolation.py',
    'tests/runners/test_adk_a2a_stream_review.py',
    'tests/runners/test_adk_multi_llm_runtime_events.py',
    'tests/runners/test_langchain_hitl_langgraph.py',
    'tests/runners/test_langgraph_explicit_answer_contract.py',
    'tests/server/test_app_factory_a2a.py',
    'tests/server/test_app_factory_agui.py',
    'tests/server/test_ui_bootstrap_agui.py',
    'tests/studio/runtime_adapter_fixtures.py',
    'tests/studio/test_a2a_bindings.py',
    'tests/studio/test_a2a_catalog.py',
    'tests/studio/test_a2a_composed_entry.py',
    'tests/studio/test_a2a_orchestration.py',
    'tests/studio/test_api.py',
    'tests/studio/test_r85_presentation.py',
    'tests/test_a2a_agent_card.py',
    'tests/test_a2a_cli.py',
    'tests/test_a2a_integration.py',
    'tests/test_a2a_toolset.py',
    'tests/test_evaluation_a2a_adapter.py',
    'tests/test_mcp_oauth_agent.py',
    'tests/test_mcp_oauth_catalog.py',
    'tests/test_mcp_oauth_code_config.py',
    'tests/test_mcp_oauth_declarations.py',
    'tests/test_mcp_oauth_documented_graph.py',
    'tests/test_mcp_oauth_extension.py',
    'tests/test_mcp_oauth_get_tools.py',
    'tests/test_mcp_oauth_local.py',
    'tests/test_mcp_oauth_local_storage.py',
    'tests/test_mcp_oauth_native_toolnode.py',
    'tests/test_mcp_oauth_platform.py',
    'tests/test_mcp_oauth_recovery.py',
    'tests/test_mcp_oauth_runtime_io.py',
    'tests/test_mcp_oauth_tool_node.py',
    'tests/test_mcp_oauth_tools.py',
    'tests/test_mcp_oauth_transport.py',
    'tests/test_mcp_oauth_vault_lock.py',
    'tests/test_mcp_regressions.py',
    'tests/test_mcp_runtime.py',
    'tests/test_mcp_runtime_optional_adk.py',
    'tests/test_mcp_standalone_native_toolnode.py',
}
PUBLIC_TEST_FILES.update(PUBLIC_PROTOCOL_TEST_FILES)

EXCLUDED_PREFIXES = (
    ".git/",
    ".mypy_cache/",
    ".pytest_cache/",
    ".ruff_cache/",
    ".venv/",
    ".zread/",
    "__pycache__/",
    "build/",
    "dist/",
    "docs/archive/",
    "docs/internal/",
    "docs/superpowers/",
    "examples/",
    "htmlcov/",
    "ksadk.egg-info/",
    "ksadk/server/web-ui/",
    "ksadk/server/static/",
    "ksadk/studio/static/",
    "ksadk/studio/react-ui/node_modules/",
    "ksadk/studio/react-ui/dist/",
    "site/",
)

EXCLUDED_PATHS = {
    ".pypirc",
    ".pypirc.example",
}

EXCLUDED_SUFFIXES = (
    ".pyc",
    ".pyo",
)


@dataclass(frozen=True)
class ExportPlan:
    ok: bool
    repo_root: str
    target_repository: str
    documentation: str
    export_paths: list[str]
    excluded_paths: list[str]
    violations: list[str]

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def normalize(path: str | Path) -> str:
    normalized = str(path).replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    while "//" in normalized:
        normalized = normalized.replace("//", "/")
    return normalized


def git_files(root: Path) -> list[str]:
    completed = subprocess.run(
        [
            "git",
            "-c",
            "core.quotePath=false",
            "ls-files",
            "--cached",
            "--others",
            "--exclude-standard",
        ],
        cwd=root,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    )
    deleted = subprocess.run(
        ["git", "-c", "core.quotePath=false", "ls-files", "--deleted"],
        cwd=root,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    )
    deleted_paths = {normalize(path) for path in deleted.stdout.splitlines()}
    return sorted(
        normalize(path)
        for path in completed.stdout.splitlines()
        if normalize(path) not in deleted_paths
    )


def git_source_provenance(root: Path) -> tuple[str, str]:
    """Return the reviewed source identity carried by a clean export."""

    commit = (
        subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            check=True,
            text=True,
            stdout=subprocess.PIPE,
        )
        .stdout.strip()
        .lower()
    )
    status = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=root,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    ).stdout.strip()
    return commit, "dirty" if status else "clean"


def filesystem_files(root: Path) -> list[str]:
    paths: list[str] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel_path = normalize(path.relative_to(root))
        paths.append(rel_path)
    return paths


def discover_files(root: Path) -> list[str]:
    if (root / ".git").exists():
        return git_files(root)
    return filesystem_files(root)


def is_excluded(path: str) -> bool:
    normalized = normalize(path)
    if not is_included_by_policy(normalized):
        return True
    if normalized in EXCLUDED_PATHS:
        return True
    if normalized.startswith("docs/reference/") and normalized not in CURATED_REFERENCE_DOCS:
        return True
    if (
        normalized.startswith("docs/")
        and normalized not in CURATED_DOCS
        and normalized not in CURATED_REFERENCE_DOCS
    ):
        return True
    if normalized.endswith(EXCLUDED_SUFFIXES):
        return True
    return any(
        normalized == prefix.rstrip("/") or normalized.startswith(prefix)
        for prefix in EXCLUDED_PREFIXES
    )


def is_included_by_policy(path: str) -> bool:
    normalized = normalize(path)
    return (
        normalized in ROOT_EXPORT_FILES
        or normalized in CURATED_DOCS
        or normalized in CURATED_REFERENCE_DOCS
        or normalized in SCRIPT_EXPORT_FILES
        or normalized in PUBLIC_TEST_FILES
        or normalized.startswith(EXPORT_PREFIXES)
    )


def build_export_plan(repo_root: Path) -> ExportPlan:
    repo_root = repo_root.resolve()
    violations: list[str] = []
    if not repo_root.is_dir():
        violations.append(f"repo root does not exist: {repo_root}")
        discovered: list[str] = []
    else:
        try:
            discovered = discover_files(repo_root)
        except subprocess.CalledProcessError as exc:
            violations.append(f"failed to discover git files: {exc}")
            discovered = []

    export_paths = sorted(
        set(path for path in discovered if not is_excluded(path))
    )
    excluded_paths = sorted(path for path in discovered if is_excluded(path))

    for required_path in sorted(REQUIRED_PUBLIC_FILES):
        if required_path not in export_paths:
            violations.append(f"missing required public file: {required_path}")

    return ExportPlan(
        ok=not violations,
        repo_root=str(repo_root),
        target_repository=TARGET_REPOSITORY,
        documentation=DOCUMENTATION_URL,
        export_paths=export_paths,
        excluded_paths=excluded_paths,
        violations=violations,
    )


def copy_export(plan: ExportPlan, output_dir: Path) -> None:
    repo_root = Path(plan.repo_root)
    source_commit, source_tree = git_source_provenance(repo_root)
    if output_dir.exists():
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True)

    for rel_path in plan.export_paths:
        source = repo_root / rel_path
        if not source.is_file():
            continue
        target = output_dir / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)

    policy_payload = {
        "rootFiles": sorted(ROOT_EXPORT_FILES),
        "prefixes": list(EXPORT_PREFIXES),
        "curatedDocs": sorted(CURATED_DOCS),
        "curatedReferenceDocs": sorted(CURATED_REFERENCE_DOCS),
        "scripts": sorted(SCRIPT_EXPORT_FILES),
        "tests": sorted(PUBLIC_TEST_FILES),
    }
    policy_digest = hashlib.sha256(
        json.dumps(
            policy_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    manifest = {
        "schemaVersion": 1,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "sourceCommit": source_commit,
        "sourceTree": source_tree,
        "targetRepository": TARGET_REPOSITORY,
        "documentation": DOCUMENTATION_URL,
        "exportPathCount": len(plan.export_paths),
        "exportPolicy": {
            "mode": "allowlist",
            "schemaVersion": 1,
            "sha256": policy_digest,
        },
    }
    (output_dir / "export-manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def format_summary(plan: ExportPlan) -> list[str]:
    return [
        f"ok: {str(plan.ok).lower()}",
        f"target repository: {plan.target_repository}",
        f"documentation: {plan.documentation}",
        f"export paths: {len(plan.export_paths)}",
        f"excluded paths: {len(plan.excluded_paths)}",
    ]


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=REPO_ROOT)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--json", action="store_true", help="print machine-readable JSON")
    parser.add_argument(
        "--summary",
        action="store_true",
        help="print a concise human-readable summary",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    plan = build_export_plan(args.repo_root)

    if plan.ok and args.output_dir:
        copy_export(plan, args.output_dir)

    payload = plan.to_dict()
    if args.output_dir:
        payload["outputDir"] = str(args.output_dir.resolve())

    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print("\n".join(format_summary(plan)))
        for violation in plan.violations:
            print(f"violation: {violation}", file=sys.stderr)

    return 0 if plan.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

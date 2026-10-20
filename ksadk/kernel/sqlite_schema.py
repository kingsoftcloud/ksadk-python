"""SQLite Kernel schema, versioned with the existing migration contract."""

from __future__ import annotations

SCHEMA_VERSION = 4

_SCHEMA = """
CREATE TABLE IF NOT EXISTS kernel_inbox (
  message_id TEXT PRIMARY KEY,
  agent_instance_id TEXT NOT NULL,
  session_id TEXT NOT NULL,
  idempotency_key TEXT NOT NULL,
  request_digest TEXT NOT NULL,
  accepted_seq INTEGER NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('accepted','claimed','completed','discarded')),
  claimed_fence INTEGER,
  payload_json TEXT NOT NULL,
  UNIQUE(session_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_kernel_inbox_claim
  ON kernel_inbox (agent_instance_id, session_id, status, accepted_seq);
CREATE INDEX IF NOT EXISTS idx_kernel_inbox_idempotency
  ON kernel_inbox (session_id, idempotency_key);

CREATE TABLE IF NOT EXISTS kernel_runs (
  run_id TEXT PRIMARY KEY,
  agent_instance_id TEXT NOT NULL,
  session_id TEXT NOT NULL,
  state TEXT NOT NULL CHECK (state IN (
    'pending','running','paused','waiting','completed','failed','cancelled','interrupted'
  )),
  activation_fence INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  metadata_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_kernel_runs_session_state
  ON kernel_runs (session_id, state);

CREATE TABLE IF NOT EXISTS kernel_activations (
  agent_instance_id TEXT NOT NULL,
  session_id TEXT NOT NULL,
  activation_id TEXT NOT NULL,
  fencing_token INTEGER NOT NULL,
  lease_expires_at REAL NOT NULL,
  lease_expires_at_iso TEXT NOT NULL,
  released INTEGER NOT NULL DEFAULT 0,
  runtime_type TEXT NOT NULL DEFAULT 'ksadk',
  bundle_digest TEXT NOT NULL DEFAULT '',
  capability_digest TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (agent_instance_id, session_id)
);
CREATE INDEX IF NOT EXISTS idx_kernel_activations_expiry
  ON kernel_activations (lease_expires_at);

CREATE TABLE IF NOT EXISTS kernel_accepted_seq (
  session_id TEXT PRIMARY KEY,
  last_seq INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS kernel_interactions (
  interaction_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  agent_instance_id TEXT NOT NULL,
  session_id TEXT NOT NULL,
  run_id TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('approval','structured_input','plan_review','custom')),
  request_schema_json TEXT NOT NULL,
  presentation_json TEXT,
  revision INTEGER NOT NULL,
  status TEXT NOT NULL CHECK (status IN (
    'pending','resolving','resolved','cancelled','expired'
  )),
  created_at TEXT NOT NULL,
  expires_at TEXT,
  provider_id TEXT NOT NULL DEFAULT '',
  native_target_json TEXT,
  continuation_json TEXT,
  request_digest TEXT NOT NULL,
  response_json TEXT,
  outcome TEXT,
  actor TEXT,
  event_id TEXT,
  accepted_seq INTEGER,
  fencing_token INTEGER,
  updated_at TEXT,
  PRIMARY KEY (tenant_id, interaction_id)
);
CREATE INDEX IF NOT EXISTS idx_kernel_interactions_pending
  ON kernel_interactions (tenant_id, session_id, status);

CREATE TABLE IF NOT EXISTS kernel_interaction_submissions (
  tenant_id TEXT NOT NULL,
  interaction_id TEXT NOT NULL,
  idempotency_key TEXT NOT NULL,
  submission_digest TEXT NOT NULL,
  receipt_json TEXT NOT NULL,
  PRIMARY KEY (tenant_id, interaction_id, idempotency_key)
);
"""

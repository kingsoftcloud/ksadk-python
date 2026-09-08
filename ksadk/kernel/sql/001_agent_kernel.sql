-- Agent Kernel durable state (Phase 1 Task 4).
-- BIGINT fencing_token / TIMESTAMPTZ lease / JSONB payload.
-- Idempotency: (tenant_id, session_id, idempotency_key) unique.
-- Claim ordering: FOR UPDATE SKIP LOCKED ordered by accepted_seq (see postgres_store.py).

CREATE TABLE IF NOT EXISTS kernel_inbox (
  message_id UUID PRIMARY KEY,
  tenant_id TEXT NOT NULL DEFAULT 'default',
  agent_instance_id TEXT NOT NULL,
  session_id TEXT NOT NULL,
  idempotency_key TEXT NOT NULL,
  request_digest TEXT NOT NULL,
  accepted_seq BIGINT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('accepted','claimed','completed','discarded')),
  claimed_fence BIGINT,
  payload JSONB NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, session_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_kernel_inbox_claim
  ON kernel_inbox (agent_instance_id, session_id, status, accepted_seq);

CREATE TABLE IF NOT EXISTS kernel_runs (
  run_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL DEFAULT 'default',
  agent_instance_id TEXT NOT NULL,
  session_id TEXT NOT NULL,
  state TEXT NOT NULL CHECK (state IN (
    'pending','running','paused','waiting','completed','failed','cancelled','interrupted'
  )),
  activation_fence BIGINT NOT NULL DEFAULT 0,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS idx_kernel_runs_session_state
  ON kernel_runs (session_id, state);

-- Activation lease: one row per (agent_instance_id, session_id). Takeover uses
-- INSERT .. ON CONFLICT .. DO UPDATE .. WHERE lease_expires_at <= now()
-- (or released / same activation) and atomically bumps fencing_token + 1.
CREATE TABLE IF NOT EXISTS kernel_activations (
  agent_instance_id TEXT NOT NULL,
  session_id TEXT NOT NULL,
  activation_id TEXT NOT NULL,
  fencing_token BIGINT NOT NULL,
  lease_expires_at TIMESTAMPTZ NOT NULL,
  released BOOLEAN NOT NULL DEFAULT FALSE,
  runtime_type TEXT NOT NULL DEFAULT 'ksadk',
  bundle_digest TEXT NOT NULL DEFAULT '',
  capability_digest TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (agent_instance_id, session_id)
);
CREATE INDEX IF NOT EXISTS idx_kernel_activations_expiry
  ON kernel_activations (lease_expires_at);

CREATE TABLE IF NOT EXISTS kernel_accepted_seq (
  tenant_id TEXT NOT NULL DEFAULT 'default',
  session_id TEXT NOT NULL,
  last_seq BIGINT NOT NULL DEFAULT 0,
  PRIMARY KEY (tenant_id, session_id)
);

-- Mutation permit nonce 单次使用（durable replay 防护，跨 Pod / 重启共享）。
-- register 语义见 postgres_store.PostgresNonceStore：INSERT .. ON CONFLICT DO
-- NOTHING，冲突时读回 (command_id, idempotency_key) 判定网络重试 vs 重放。
CREATE TABLE IF NOT EXISTS kernel_permit_nonces (
  nonce TEXT PRIMARY KEY,
  command_id TEXT NOT NULL,
  idempotency_key TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_kernel_permit_nonces_created
  ON kernel_permit_nonces (created_at);

-- Durable Interaction ledger (Phase 1 Task 5). First-wins terminal CAS on
-- (revision, status); terminal decision and its SessionEvent append happen in
-- the same writer transaction (see postgres_store interaction methods).
-- Row key: (tenant_id, interaction_id); idempotency submissions are unique on
-- (tenant_id, interaction_id, idempotency_key).
CREATE TABLE IF NOT EXISTS kernel_interactions (
  interaction_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL DEFAULT 'default',
  agent_instance_id TEXT NOT NULL,
  session_id TEXT NOT NULL,
  run_id TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('approval','structured_input','plan_review','custom')),
  request_schema JSONB NOT NULL,
  presentation JSONB,
  revision BIGINT NOT NULL,
  status TEXT NOT NULL CHECK (status IN (
    'pending','resolving','resolved','cancelled','expired'
  )),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  expires_at TIMESTAMPTZ,
  provider_id TEXT NOT NULL DEFAULT '',
  native_target JSONB,
  continuation_metadata JSONB,
  request_digest TEXT NOT NULL,
  response JSONB,
  outcome TEXT,
  actor TEXT,
  event_id UUID,
  accepted_seq BIGINT,
  fencing_token BIGINT,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, interaction_id)
);
CREATE INDEX IF NOT EXISTS idx_kernel_interactions_pending
  ON kernel_interactions (tenant_id, session_id, status);

CREATE TABLE IF NOT EXISTS kernel_interaction_submissions (
  tenant_id TEXT NOT NULL DEFAULT 'default',
  interaction_id TEXT NOT NULL,
  idempotency_key TEXT NOT NULL,
  submission_digest TEXT NOT NULL,
  receipt JSONB NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, interaction_id, idempotency_key)
);

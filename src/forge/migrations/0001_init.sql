-- Initial schema.
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS jobs (
  id BIGSERIAL PRIMARY KEY,
  kind TEXT NOT NULL,
  payload TEXT NOT NULL DEFAULT '{}',
  idem_key TEXT UNIQUE,
  status TEXT NOT NULL DEFAULT 'queued',   -- queued | running | done | dead
  priority INTEGER NOT NULL DEFAULT 0,
  attempts INTEGER NOT NULL DEFAULT 0,
  max_attempts INTEGER NOT NULL DEFAULT 3,
  lease_owner TEXT,
  lease_expires DOUBLE PRECISION,
  run_after DOUBLE PRECISION NOT NULL DEFAULT 0,
  published DOUBLE PRECISION,              -- outbox: when this job was last handed to NATS
  result TEXT,
  error TEXT,
  created DOUBLE PRECISION NOT NULL,
  updated DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS jobs_claim ON jobs(status, kind, priority DESC, id);

CREATE TABLE IF NOT EXISTS effects (idem_key TEXT PRIMARY KEY, job_id BIGINT, created DOUBLE PRECISION);
CREATE TABLE IF NOT EXISTS effects_naive (id BIGSERIAL PRIMARY KEY, idem_key TEXT, job_id BIGINT);

CREATE TABLE IF NOT EXISTS repos (
  name TEXT PRIMARY KEY, url TEXT, head TEXT, indexed_head TEXT,
  mined_at DOUBLE PRECISION, indexed_at DOUBLE PRECISION
);

CREATE TABLE IF NOT EXISTS tasks (
  id TEXT PRIMARY KEY, repo TEXT, sha TEXT, parent TEXT, title TEXT, commit_msg TEXT,
  issue TEXT, gold_patch TEXT, test_patch TEXT,
  src_files TEXT, test_files TEXT, symbols TEXT, lines_changed INTEGER,
  status TEXT DEFAULT 'candidate',   -- candidate | valid | rejected
  reject_reason TEXT, f2p TEXT, p2p TEXT, judge TEXT,
  mutation_score DOUBLE PRECISION, mutants TEXT, image TEXT,
  created DOUBLE PRECISION, validated_at DOUBLE PRECISION
);

CREATE TABLE IF NOT EXISTS rollouts (
  id BIGSERIAL PRIMARY KEY, task_id TEXT, parent_id BIGINT, fork_step INTEGER,
  kind TEXT DEFAULT 'agent',   -- agent | redteam
  status TEXT DEFAULT 'queued', model TEXT, temperature DOUBLE PRECISION, seed INTEGER,
  steps INTEGER DEFAULT 0, stop_reason TEXT, patch TEXT, final_image TEXT,
  naive_pass INTEGER, robust_pass INTEGER, flags TEXT, verdict TEXT, judge TEXT, env_diff TEXT, naive_log TEXT,
  prompt_tokens INTEGER DEFAULT 0, completion_tokens INTEGER DEFAULT 0,
  error TEXT, created DOUBLE PRECISION, finished DOUBLE PRECISION
);
CREATE INDEX IF NOT EXISTS rollouts_task ON rollouts(task_id);

CREATE TABLE IF NOT EXISTS snapshots (
  id BIGSERIAL PRIMARY KEY, rollout_id BIGINT, step INTEGER, image TEXT,
  messages TEXT, action TEXT, observation TEXT, commit_ms DOUBLE PRECISION, env_diff TEXT,
  created DOUBLE PRECISION,
  UNIQUE(rollout_id, step)
);

CREATE TABLE IF NOT EXISTS hack_samples (
  id BIGSERIAL PRIMARY KEY, task_id TEXT, kind TEXT, is_hack INTEGER, patch TEXT,
  naive_pass INTEGER, robust_pass INTEGER, flagged INTEGER, static_flagged INTEGER, flags TEXT,
  UNIQUE(task_id, kind)
);

-- code index: one row per function/class; vector search via pgvector, near-duplicates via MinHash LSH bands
CREATE TABLE IF NOT EXISTS chunks (
  id BIGSERIAL PRIMARY KEY, repo TEXT, path TEXT, symbol TEXT, kind TEXT,
  start_line INTEGER, end_line INTEGER, text TEXT, emb vector(768), minhash BYTEA,
  UNIQUE(repo, path, symbol, start_line)
);
CREATE INDEX IF NOT EXISTS chunks_emb ON chunks USING hnsw (emb vector_cosine_ops);
CREATE TABLE IF NOT EXISTS chunk_bands (
  chunk_id BIGINT REFERENCES chunks(id) ON DELETE CASCADE, band SMALLINT, key BIGINT,
  PRIMARY KEY(band, key, chunk_id)
);

CREATE TABLE IF NOT EXISTS envs (
  id BIGSERIAL PRIMARY KEY, name TEXT, status TEXT, network TEXT, nodes TEXT,
  from_snapshot BIGINT, created DOUBLE PRECISION
);
CREATE TABLE IF NOT EXISTS env_snapshots (
  id BIGSERIAL PRIMARY KEY, env_id BIGINT, images TEXT, state TEXT, ms DOUBLE PRECISION,
  created DOUBLE PRECISION
);

-- live worker processes (each upserts its row every few seconds) and cluster-wide switches
CREATE TABLE IF NOT EXISTS workers (
  owner TEXT PRIMARY KEY, concurrency INTEGER, active TEXT, seen DOUBLE PRECISION
);
CREATE TABLE IF NOT EXISTS control (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS bench (name TEXT PRIMARY KEY, data TEXT, created DOUBLE PRECISION);
CREATE TABLE IF NOT EXISTS sim_checkpoints (agent INTEGER, step INTEGER, state TEXT, PRIMARY KEY(agent, step));

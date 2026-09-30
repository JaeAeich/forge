-- Idempotent rollout creation: a deterministic key per rollout, so a retried job converges instead of
-- duplicating (or losing) rollouts. NULL for ad-hoc rollouts (e.g. started from the UI).
ALTER TABLE rollouts ADD COLUMN IF NOT EXISTS idem_key TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS rollouts_idem_key ON rollouts(idem_key);

-- backfill the keys the pipeline would have used: first root per (task, kind), forks by position
UPDATE rollouts r SET idem_key = 'task:' || r.task_id || ':' || r.kind
WHERE r.parent_id IS NULL AND r.idem_key IS NULL
  AND r.id = (SELECT min(id) FROM rollouts x WHERE x.task_id = r.task_id AND x.kind = r.kind AND x.parent_id IS NULL);
UPDATE rollouts r SET idem_key = 'fork:' || f.parent_id || ':' || f.fork_step || ':' || f.i
FROM (
  SELECT id, parent_id, fork_step, row_number() OVER (PARTITION BY parent_id, fork_step ORDER BY id) - 1 AS i
  FROM rollouts WHERE parent_id IS NOT NULL AND fork_step IS NOT NULL
) f
WHERE r.id = f.id AND r.idem_key IS NULL;

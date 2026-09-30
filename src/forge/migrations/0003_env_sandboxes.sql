-- Environments run as one multi-node sandbox (Docker or Kubernetes), addressed by its id; a snapshot is a ref.
ALTER TABLE envs RENAME COLUMN network TO sandbox;
ALTER TABLE envs DROP COLUMN nodes;
ALTER TABLE env_snapshots RENAME COLUMN images TO ref;
-- rows from before this migration point at containers/images in the old format: retire them
UPDATE envs SET status='destroyed', sandbox=NULL WHERE status!='destroyed';
DELETE FROM env_snapshots;

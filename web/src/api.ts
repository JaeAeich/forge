// Typed client for the forge HTTP API (src/forge/server.py).

export type Verdict = "clean" | "suspect" | "hack" | null;
export type RolloutStatus = "queued" | "running" | "done" | "graded";
export type TaskStatus = "candidate" | "valid" | "rejected";
export type JobStatus = "queued" | "running" | "done" | "dead";
export type Flag = { rule: string; severity: "high" | "medium" | "info"; evidence: string };
export type Payload = { task?: string; repo?: string; rollout?: number };

export interface Overview {
  models: { coder: string; embed: string; ollama: string[] | null };
  tasks: Partial<Record<TaskStatus, number>>;
  rollouts: { kind: string; status: RolloutStatus; verdict: Verdict; robust_pass: number | null; n: number }[];
  jobs: Record<string, Partial<Record<JobStatus, number>>>;
  chunks: number;
  repos: { name: string; mined_at: number | null; indexed_at: number | null }[];
}

export interface Activity {
  id: number;
  kind: string;
  payload: Payload;
  status: JobStatus;
  result: Record<string, unknown> | null;
  error: string | null;
  updated: number;
}

export type Workers =
  | { present: false }
  | {
      present: true;
      paused: boolean;
      count: number;
      concurrency: number;
      active: { id: number; kind: string; payload: Payload; attempts: number }[];
    };

export interface TaskRow {
  id: string;
  repo: string;
  title: string;
  status: TaskStatus;
  reject_reason: string | null;
  mutation_score: number | null;
  f2p: number;
  rollouts: number;
  solved: number;
}

export interface RolloutBrief {
  id: number;
  parent_id: number | null;
  fork_step: number | null;
  kind: "agent" | "redteam";
  status: RolloutStatus;
  steps: number;
  robust_pass: number | null;
  naive_pass?: number | null;
  verdict: Verdict;
}

export interface Task {
  id: string;
  repo: string;
  sha: string;
  title: string;
  status: TaskStatus;
  reject_reason: string | null;
  issue: string | null;
  commit_msg: string;
  gold_patch: string;
  test_patch: string;
  f2p: string[] | null;
  p2p: string[] | null;
  judge: { solvable?: number; reason?: string } | null;
  mutation_score: number | null;
  mutants: { file: string; op: string; line: number; killed: boolean }[] | null;
  rollouts: RolloutBrief[];
  hacks: { kind: string; is_hack: number; naive_pass: number; robust_pass: number; flagged: number }[];
}

export interface RolloutRow extends RolloutBrief {
  task_id: string;
  flags: string[];
}

export interface Step {
  step: number;
  action: { tool: string | null; args?: Record<string, string | number>; text?: string } | null;
  observation: string | null;
  commit_ms?: number | null; // absent on inherited prefix steps
}

/** Node of a rollout's fork tree: the server's tree query omits kind and naive_pass. */
export type TreeNode = Omit<RolloutBrief, "kind" | "naive_pass">;

export interface Rollout extends RolloutBrief {
  task_id: string;
  stop_reason: string | null;
  patch: string | null;
  naive_log: string | null;
  flags: Flag[] | null;
  env_diff: string[] | null;
  judge: { reason?: string; grader?: { failed?: string[] } } | null;
  steps_detail: Step[];
  prefix?: Step[];
  tree: TreeNode[];
  tree_root: number;
}

export interface Corpus {
  samples: number;
  tasks: number;
  naive_false_pass_rate: number | null;
  robust_false_pass_rate: number | null;
  detector_recall: number | null;
  detector_precision: number | null;
  false_positive_rate: number | null;
  by_kind: Record<string, { n: number; naive_pass: number; robust_pass: number; flagged: number }>;
}

export interface QA {
  corpus: Corpus;
  flagged_rollouts: { id: number; task_id: string; kind: string; verdict: Verdict; flags: Flag[] }[];
}

export interface SearchResult {
  results: { repo: string; path: string; symbol: string; text: string; score: number }[];
  corpus: number;
}

export interface Contamination {
  verdict: "novel" | "similar" | "near-duplicate";
  matches: { repo: string; path: string; symbol: string; jaccard: number; cosine: number }[];
}

export interface IndexStats {
  repos: { repo: string; chunks: number; indexed_at: number | null }[];
  near_duplicates: { pairs: number; cross_repo: number; examples: { a: string; b: string; jaccard: number }[] };
}

export interface Envs {
  envs: { id: number; status: string; from_snapshot: number | null }[];
  snapshots: { id: number; env_id: number; state: string; created: number }[];
}
export type EnvState = { pg_orders: number; redis_counter: number };

export interface Job {
  id: number;
  kind: string;
  payload: string;
  status: JobStatus;
  attempts: number;
  error: string | null;
  updated: number;
}

export type Bench = Record<string, Record<string, unknown>>;

export async function get<T>(path: string): Promise<T> {
  const r = await fetch(`/api${path}`);
  if (!r.ok) throw new Error((await r.text()).slice(0, 200));
  return (await r.json()) as T;
}

export async function send<T>(path: string, body: unknown = {}, method = "POST"): Promise<T> {
  const r = await fetch(`/api${path}`, {
    method,
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!r.ok) throw new Error((await r.text()).slice(0, 200));
  return (await r.json()) as T;
}

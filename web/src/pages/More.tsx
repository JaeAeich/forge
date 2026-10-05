import { useRef, useState } from "react";
import {
  type Bench as BenchData,
  type Contamination,
  type EnvState,
  type Envs as EnvsData,
  get,
  type IndexStats,
  type Job,
  type Payload,
  type QA as QAData,
  type SearchResult,
  send,
} from "../api";
import { ago, human, JOB_LABEL, jobTarget, kindLabel, pct, toast, useApi } from "../lib";
import {
  Act,
  Card,
  Code,
  Dot,
  Empty,
  type Filter,
  FilterList,
  Fold,
  Head,
  Loading,
  Pad,
  Pill,
  Stat,
  Table,
} from "../ui";

// ---------- QA ----------
export function QA() {
  const { data, error } = useApi<QAData>("/qa", 30000);
  if (!data) return <Loading error={error} />;
  const c = data.corpus;
  return (
    <>
      <Head title="QA" />
      <div className="stats">
        <Stat label="Naive grader fooled" value={pct(c.naive_false_pass_rate)} sub="by injected exploits" />
        <Stat label="Robust grader fooled" value={pct(c.robust_false_pass_rate)} sub="by injected exploits" />
        <Stat label="Detector recall" value={pct(c.detector_recall)} sub={`precision ${pct(c.detector_precision)}`} />
        <Stat label="False alarms" value={pct(c.false_positive_rate)} sub="on correct fixes" />
      </div>
      <Card title="Flagged rollouts">
        <Table
          rows={data.flagged_rollouts}
          href={(r) => `#/rollout/${r.id}`}
          keyboard="qa"
          empty="Nothing flagged"
          cols={[
            { head: "#", cell: (r) => `#${r.id}` },
            { head: "Task", cell: (r) => <span className="soft">{r.task_id}</span> },
            { head: "Kind", cell: kindLabel },
            {
              head: "Why",
              cell: (r) =>
                r.flags
                  .filter((f) => f.severity !== "info")
                  .map((f) => human(f.rule))
                  .join(", "),
              className: "wrap",
            },
            {
              cell: (r) => (
                <Pill tone={r.verdict === "hack" ? "bad" : "warn"}>{r.verdict === "hack" ? "Hack" : "Suspect"}</Pill>
              ),
              className: "r",
            },
          ]}
        />
      </Card>
      <Fold title="By exploit" extra={`${c.samples} samples`}>
        <Table
          rows={Object.entries(c.by_kind)}
          cols={[
            { head: "Exploit", cell: ([k]) => human(k) },
            { head: "Samples", cell: ([, v]) => v.n, className: "r" },
            { head: "Naive passed", cell: ([, v]) => v.naive_pass, className: "r" },
            { head: "Robust passed", cell: ([, v]) => v.robust_pass, className: "r" },
            { head: "Detected", cell: ([, v]) => v.flagged, className: "r" },
          ]}
        />
      </Fold>
    </>
  );
}

// ---------- code index ----------
/** Only the newest call's result is applied (typing + Enter can overlap); failures toast. */
function useLatest<T>(set: (v: T) => void) {
  const seq = useRef(0);
  return async (fn: () => Promise<T>) => {
    const id = ++seq.current;
    try {
      const v = await fn();
      if (id === seq.current) set(v);
    } catch (e) {
      if (id === seq.current) toast(`Error: ${(e as Error).message}`);
    }
  };
}

export function CodeIndex() {
  const { data: stats } = useApi<IndexStats>("/index");
  const [q, setQ] = useState("");
  const [results, setResults] = useState<SearchResult | null>(null);
  const [snippet, setSnippet] = useState("");
  const [contam, setContam] = useState<Contamination | null>(null);
  const searchLatest = useLatest(setResults);
  const checkLatest = useLatest(setContam);
  const search = () => searchLatest(() => get<SearchResult>(`/search?q=${encodeURIComponent(q)}`));
  const check = () => checkLatest(() => send<Contamination>("/contamination", { code: snippet }));
  return (
    <>
      <Head title="Code index" />
      <Card>
        <Pad>
          <div className="row">
            <input
              id="sq"
              placeholder="Search code by meaning"
              aria-label="Search code by meaning"
              value={q}
              onChange={(e) => setQ(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && void search()}
            />
            <button type="button" className="p" onClick={search}>
              Search
            </button>
          </div>
        </Pad>
        {results?.results.map((x, i) => (
          <details key={`${x.repo}${x.path}${x.symbol}`} className="step">
            <summary>
              <span className="i">{i + 1}</span>
              <span className="w">
                <b>{x.symbol}</b>{" "}
                <span className="mut">
                  {x.repo}/{x.path}
                </span>
              </span>
              <span className="mut">{x.score.toFixed(2)}</span>
            </summary>
            <div className="body">
              <Code text={x.text} lang="python" />
            </div>
          </details>
        ))}
        {results && !results.results.length ? <Empty>No results</Empty> : null}
      </Card>
      <Fold title="Contamination check">
        <Pad>
          <textarea
            placeholder="Paste code"
            aria-label="Code to check for contamination"
            value={snippet}
            onChange={(e) => setSnippet(e.target.value)}
          />
          <div className="row" style={{ marginTop: 8 }}>
            <button type="button" onClick={check}>
              Check
            </button>
            {contam ? <Dot tone={contam.verdict === "novel" ? "ok" : "bad"}>{contam.verdict}</Dot> : null}
          </div>
        </Pad>
        {contam ? (
          <Table
            rows={contam.matches}
            cols={[
              {
                head: "Closest matches",
                cell: (x) => (
                  <>
                    <b>{x.symbol}</b>{" "}
                    <span className="mut">
                      {x.repo}/{x.path}
                    </span>
                  </>
                ),
              },
              { head: "Overlap", cell: (x) => pct(x.jaccard), className: "r" },
              { head: "Similarity", cell: (x) => x.cosine.toFixed(2), className: "r" },
            ]}
          />
        ) : null}
      </Fold>
      {stats ? (
        <>
          <Fold title="Indexed repos" extra={stats.repos.reduce((a, r) => a + r.chunks, 0).toLocaleString()}>
            <Table
              rows={stats.repos}
              cols={[
                { head: "Repo", cell: (r) => r.repo },
                { head: "Chunks", cell: (r) => r.chunks.toLocaleString(), className: "r" },
                { head: "Indexed", cell: (r) => ago(r.indexed_at), className: "soft" },
              ]}
            />
          </Fold>
          <Fold title="Near-duplicate code" extra={`${stats.near_duplicates.pairs} pairs`}>
            <Table
              rows={stats.near_duplicates.examples}
              empty="None found"
              cols={[
                { head: "A", cell: (r) => <code>{r.a}</code>, className: "wrap" },
                { head: "B", cell: (r) => <code>{r.b}</code>, className: "wrap" },
                { head: "Overlap", cell: (r) => pct(r.jaccard), className: "r" },
              ]}
            />
          </Fold>
        </>
      ) : null}
    </>
  );
}

// ---------- environments ----------
function LiveState({ id }: { id: number }) {
  const { data: s, error } = useApi<EnvState>(`/envs/${id}/state`);
  if (error) return <span className="no">unreachable</span>;
  return <code>{s ? `${s.pg_orders} orders · redis ${s.redis_counter}` : "…"}</code>;
}

export function Envs() {
  const { data, error } = useApi<EnvsData>("/envs");
  if (!data) return <Loading error={error} />;
  const live = data.envs.filter((e) => e.status === "running");
  return (
    <>
      <Head
        title="Environments"
        actions={
          <Act className="p" run={() => send<{ id: number }>("/envs")} msg={(r) => `Stack #${r.id} started`}>
            New stack
          </Act>
        }
      />
      <Card title="Running">
        <Table
          rows={live}
          empty="None running"
          cols={[
            {
              cell: (e) => (
                <>
                  #{e.id} {e.from_snapshot ? <span className="mut">from S{e.from_snapshot}</span> : null}
                </>
              ),
            },
            { cell: (e) => <LiveState id={e.id} /> },
            {
              className: "r",
              cell: (e) => (
                <div className="row end">
                  <Act className="sm" run={() => send(`/envs/${e.id}/write`, { n: 10 })} msg="Wrote 10 orders">
                    +10 orders
                  </Act>
                  <Act
                    className="sm"
                    run={() => send<{ snapshot: number }>(`/envs/${e.id}/snapshot`)}
                    msg={(x) => `Snapshot S${x.snapshot}`}
                  >
                    Snapshot
                  </Act>
                  <Act
                    className="sm"
                    confirm={`Destroy stack #${e.id}?`}
                    run={() => send(`/envs/${e.id}`, {}, "DELETE")}
                    msg="Destroyed"
                  >
                    Destroy
                  </Act>
                </div>
              ),
            },
          ]}
        />
      </Card>
      <Card title="Snapshots">
        <Table
          rows={data.snapshots}
          empty="None yet"
          cols={[
            { cell: (s) => `S${s.id}` },
            {
              cell: (s) => {
                const st = JSON.parse(s.state || "{}") as Partial<EnvState>;
                return <span className="soft">{`${st.pg_orders ?? "?"} orders · ${ago(s.created)}`}</span>;
              },
            },
            {
              className: "r",
              cell: (s) => (
                <Act
                  className="sm"
                  run={() => send<{ id: number }>("/envs", { snapshot: s.id })}
                  msg={(x) => `Restored as stack #${x.id}`}
                >
                  Restore
                </Act>
              ),
            },
          ]}
        />
      </Card>
    </>
  );
}

// ---------- jobs ----------
const JOB_FILTERS: Filter<Job>[] = [
  ["all", "All", () => true],
  ["running", "Running", (j) => j.status === "running"],
  ["queued", "Queued", (j) => j.status === "queued"],
  ["dead", "Failed", (j) => j.status === "dead"],
];
const parse = (p: string): Payload => {
  try {
    return JSON.parse(p) as Payload;
  } catch {
    return {};
  }
};

export function Jobs() {
  const { data, error } = useApi<Job[]>("/jobs?limit=300", 30000);
  if (!data) return <Loading error={error} />;
  return (
    <>
      <Head title="Jobs" />
      <Card>
        <FilterList
          id="jobs"
          rows={data}
          filters={JOB_FILTERS}
          search={(j) => `${j.kind} ${j.payload} ${j.error ?? ""}`}
          empty="No jobs match"
          cols={[
            { head: "Step", cell: (j) => JOB_LABEL[j.kind] ?? j.kind },
            { head: "Target", cell: (j) => <span className="soft">{jobTarget(parse(j.payload))}</span> },
            {
              head: "Status",
              cell: (j) => (
                <Dot
                  tone={j.status === "done" ? "ok" : j.status === "dead" ? "bad" : j.status === "running" ? "info" : ""}
                >
                  {j.status}
                </Dot>
              ),
            },
            { cell: (j) => <span className="soft">{ago(j.updated)}</span> },
            {
              className: "r",
              cell: (j) =>
                j.error ? (
                  <Act className="sm" title={j.error} run={() => send(`/jobs/${j.id}/retry`)} msg="Requeued">
                    Retry
                  </Act>
                ) : null,
            },
          ]}
        />
      </Card>
    </>
  );
}

// ---------- benchmarks ----------
type KV = [string, string | number | undefined];
const Metrics = ({ rows }: { rows: KV[] }) => (
  <Table
    rows={rows}
    cols={[
      { head: "Metric", cell: ([k]) => k },
      { head: "Value", cell: ([, v]) => v ?? "–", className: "r" },
    ]}
  />
);

export function Bench() {
  const { data, error } = useApi<BenchData>("/bench");
  if (!data) return <Loading error={error} />;
  const num = (o: Record<string, unknown> | undefined, k: string) => o?.[k] as number | undefined;
  const p50 = (o: Record<string, unknown> | undefined, k: string) => (o?.[k] as { p50?: number } | undefined)?.p50;
  const c = data.chaos_tx;
  const t = data.throughput;
  const a = data.agents_sim;
  const s = data.sandboxes;
  const m = data.multinode_snapshot;
  const checks = m?.checks as Record<string, boolean> | undefined;
  const yes = (b: boolean | undefined) => (b ? "yes" : "–");
  return (
    <>
      <Head title="Benchmarks" />
      <div className="grid2">
        <Card title="Crash safety">
          <Metrics
            rows={[
              ["Jobs", num(c, "jobs")],
              ["Workers killed (SIGKILL)", num(c, "sigkills")],
              [
                "Duplicate side effects",
                `${num(c, "duplicate_effects") ?? "–"} (naive: ${num(data.chaos_naive, "duplicate_effects") ?? "–"})`,
              ],
              ["Lost jobs", num(c, "lost")],
            ]}
          />
        </Card>
        <Card title="Queue throughput">
          <Metrics
            rows={[
              ["Jobs per second", num(t, "jobs_per_s")?.toLocaleString()],
              ["Jobs", num(t, "jobs")?.toLocaleString()],
              ["Worker processes", num(t, "workers")],
              ["Job slots", num(t, "slots")],
            ]}
          />
        </Card>
        <Card title="100k simulated agents">
          <Metrics
            rows={[
              ["Agents", num(a, "agents")?.toLocaleString()],
              ["Steps", num(a, "steps_recorded")?.toLocaleString()],
              ["Exactly once after crash", yes(a?.exactly_once as boolean | undefined)],
              ["Peak memory", num(a, "peak_rss_mb") != null ? `${num(a, "peak_rss_mb")} MB` : undefined],
            ]}
          />
        </Card>
        <Card title="Sandboxes">
          <Metrics
            rows={[
              ["Start (p50)", p50(s, "start_ms") != null ? `${p50(s, "start_ms")} ms` : undefined],
              ["Snapshot (p50)", p50(s, "snapshot_ms") != null ? `${p50(s, "snapshot_ms")} ms` : undefined],
              ["Restore (p50)", p50(s, "restore_ms") != null ? `${p50(s, "restore_ms")} ms` : undefined],
              ["In parallel", num(s, "parallel")],
            ]}
          />
        </Card>
        <Card title="Multi-node snapshot">
          <Metrics
            rows={[
              ["Restore matches snapshot", yes(checks?.restore_matches_snapshot)],
              ["Forks isolated", yes(checks?.forks_isolated)],
              ["Postgres and Redis agree", yes(checks?.pg_redis_consistent)],
              [
                "Two parallel restores",
                num(m, "parallel_restore_ms") != null
                  ? `${((num(m, "parallel_restore_ms") ?? 0) / 1000).toFixed(1)} s`
                  : undefined,
              ],
            ]}
          />
        </Card>
      </div>
    </>
  );
}

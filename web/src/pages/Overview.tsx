import { type Activity, type Overview as OverviewData, send } from "../api";
import { ago, JOB_LABEL, jobHref, jobTarget, pct, plural, useApi } from "../lib";
import { Act, Card, Fold, Head, Loading, Stat, Table } from "../ui";

function describe(j: Activity) {
  const r = (j.result ?? {}) as Record<string, unknown>;
  if (j.status === "dead") return <span className="no">Failed · {j.error}</span>;
  switch (j.kind) {
    case "mine_repo":
      return plural((r.new_tasks as string[] | undefined)?.length ?? 0, "new candidate");
    case "index_repo":
      return r.skipped ? "already up to date" : `${r.chunks} chunks indexed`;
    case "validate_task":
      return r.status === "valid" ? (
        <>
          <span className="yes">valid</span> · {String(r.f2p)} failing→passing tests
        </>
      ) : (
        `rejected · ${String(r.reason ?? r.status)}`
      );
    case "grade":
      if (r.verdict === "hack")
        return (
          <>
            <span className="no">hack caught</span> · {((r.flags as string[]) ?? []).join(", ")}
          </>
        );
      return r.robust ? <span className="yes">solved</span> : "not solved";
    case "hack_corpus":
      return `${Object.values(r).filter((x) => (x as { flagged?: string }).flagged === "hack").length} exploits caught`;
    case "mutate":
      return `grader caught ${pct(r.score as number | null)} of mutants`;
    default:
      return "";
  }
}

// rollout jobs are summarised by their grade; re-checks that found nothing are noise
const interesting = (j: Activity) =>
  (j.kind !== "rollout" || j.status === "dead") &&
  !(j.kind === "mine_repo" && !((j.result?.new_tasks as unknown[] | undefined)?.length ?? 0));

export function Overview() {
  const { data, error } = useApi<OverviewData>("/overview", 30000);
  const { data: activity, error: activityError } = useApi<Activity[]>("/activity?limit=40", 30000);
  if (!data || !activity) return <Loading error={error ?? activityError} />;
  const n = (f: (r: OverviewData["rollouts"][number]) => boolean) =>
    data.rollouts.filter(f).reduce((a, r) => a + r.n, 0);
  const graded = n((r) => r.status === "graded");
  const solved = n((r) => !!r.robust_pass);
  const events = activity.filter(interesting).slice(0, 12);
  return (
    <>
      <Head
        title="Overview"
        actions={
          <Act className="p" run={() => send("/pipeline/start")} msg="Pipeline queued">
            Run pipeline
          </Act>
        }
      />
      <div className="stats">
        <Stat
          label="Valid tasks"
          value={data.tasks.valid ?? 0}
          sub={`${data.tasks.rejected ?? 0} rejected`}
          href="#/tasks"
        />
        <Stat label="Rollouts" value={graded} sub={`${n(() => true) - graded} pending`} href="#/rollouts" />
        <Stat label="Solved" value={solved} sub={graded ? pct(solved / graded) : ""} href="#/rollouts" />
        <Stat label="Hacks caught" value={n((r) => r.verdict === "hack")} href="#/qa" />
      </div>
      <Card title="Activity">
        <Table
          rows={events}
          href={(j) => jobHref(j.payload)}
          empty="Nothing yet. Press Run pipeline."
          cols={[
            { cell: (j) => <span className="soft">{ago(j.updated)}</span> },
            { cell: (j) => JOB_LABEL[j.kind] ?? j.kind },
            { cell: (j) => jobTarget(j.payload) },
            { cell: describe, className: "wrap" },
          ]}
        />
      </Card>
      <Fold title="Repos" extra={data.repos.length}>
        <Table
          rows={data.repos}
          cols={[
            { head: "Repo", cell: (r) => r.name },
            { head: "Mined", cell: (r) => ago(r.mined_at), className: "soft" },
            { head: "Indexed", cell: (r) => ago(r.indexed_at), className: "soft" },
          ]}
        />
      </Fold>
    </>
  );
}

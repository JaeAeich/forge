import type { ReactNode } from "react";
import { type Rollout, type RolloutRow, type Step, send, type TreeNode } from "../api";
import { human, kindLabel, outcome, plural, useApi } from "../lib";
import {
  Act,
  Card,
  Code,
  Diff,
  Dot,
  Empty,
  type Filter,
  FilterList,
  Fold,
  Head,
  Loading,
  Pad,
  Pass,
  Result,
  Status,
  Table,
} from "../ui";

const FILTERS: Filter<RolloutRow>[] = [
  ["all", "All", () => true],
  ["solved", "Solved", (r) => !!r.robust_pass],
  ["hack", "Hacks", (r) => r.verdict === "hack"],
  ["live", "In progress", (r) => r.status !== "graded"],
  ["redteam", "Red team", (r) => r.kind === "redteam"],
];

export function Rollouts() {
  const { data, error } = useApi<RolloutRow[]>("/rollouts", 30000);
  if (!data) return <Loading error={error} />;
  return (
    <>
      <Head title="Rollouts" />
      <Card>
        <FilterList
          id="rollouts"
          rows={data}
          filters={FILTERS}
          search={(r) => `#${r.id} ${r.task_id} ${r.kind}`}
          href={(r) => `#/rollout/${r.id}`}
          empty="No rollouts match"
          cols={[
            { head: "#", cell: (r) => `#${r.id}` },
            { head: "Task", cell: (r) => <span className="soft">{r.task_id}</span> },
            {
              head: "Kind",
              cell: (r) => (
                <>
                  {kindLabel(r)} {r.parent_id ? <span className="mut">fork</span> : null}
                </>
              ),
            },
            { head: "Result", cell: (r) => <Result r={r} />, className: "r" },
          ]}
        />
      </Card>
    </>
  );
}

/** One-line summary of an agent action, e.g. the bash command or the file it viewed. */
function summary(a: Step["action"]): string {
  if (!a?.tool) return (a?.text ?? "").slice(0, 160);
  const args = a.args ?? {};
  if (a.tool === "bash") return String(args.command ?? "");
  if (a.tool === "view") return `${args.path ?? ""}${args.start ? `:${args.start}` : ""}`;
  if (a.tool === "submit") return "";
  return String(args.path ?? "");
}

function StepRow({ s, rolloutId, inherited }: { s: Step; rolloutId: number; inherited: boolean }) {
  const a = s.action;
  const args = a?.args ?? {};
  let body: ReactNode = null;
  if (a?.tool === "bash") body = <Code text={String(args.command ?? "")} lang="bash" />;
  else if (a?.tool === "replace")
    body = (
      <>
        <Code text={String(args.old ?? "")} lang="python" />
        <div className="lbl">↓</div>
        <Code text={String(args.new ?? "")} lang="python" />
      </>
    );
  else if (a?.tool === "write") body = <Code text={String(args.content ?? "")} lang="python" />;
  else if (a?.text) body = <Code text={a.text} />;
  return (
    <details className={`step ${inherited ? "inh" : ""}`}>
      <summary>
        <span className="i">{s.step}</span>
        <span className="t">{a?.tool ?? "–"}</span>
        <span className="w">{summary(a)}</span>
        {inherited ? null : (
          <Act
            className="sm fork"
            run={() => send<{ rollouts: number[] }>(`/rollouts/${rolloutId}/fork`, { step: s.step, n: 2 })}
            msg={(x) => `Forked into #${x.rollouts.join(", #")}`}
          >
            Fork
          </Act>
        )}
      </summary>
      <div className="body">
        {body}
        {s.observation ? <Code text={s.observation} lang={a?.tool === "view" ? "python" : "plaintext"} /> : null}
      </div>
    </details>
  );
}

function Tree({ nodes, root, current }: { nodes: TreeNode[]; root: number; current: number }) {
  const kids = new Map<number | null, TreeNode[]>();
  for (const n of nodes) kids.set(n.parent_id, [...(kids.get(n.parent_id) ?? []), n]);
  const node = (n: TreeNode): ReactNode => (
    <li key={n.id} className={n.id === current ? "cur" : ""}>
      <a href={`#/rollout/${n.id}`}>#{n.id}</a>{" "}
      <span className="mut">
        {n.fork_step != null ? `@${n.fork_step} · ` : ""}
        {n.steps} steps
      </span>{" "}
      <Result r={n} />
      {kids.get(n.id) ? <ul>{kids.get(n.id)?.map(node)}</ul> : null}
    </li>
  );
  const r = nodes.find((n) => n.id === root);
  return <div className="tree">{r ? <ul>{node(r)}</ul> : null}</div>;
}

export function RolloutPage({ id }: { id: string }) {
  const { data: r, error } = useApi<Rollout>(`/rollouts/${id}`);
  if (!r) return <Loading error={error} />;
  const failed = r.judge?.grader?.failed ?? [];
  const flags = (r.flags ?? []).filter((f) => f.severity !== "info");
  const [label, tone] = outcome(r);
  const why =
    r.verdict === "hack"
      ? flags.map((f) => human(f.rule)).join(", ")
      : r.robust_pass
        ? "passes every hidden test"
        : failed.length
          ? `${plural(failed.length, "hidden test")} failing`
          : "";
  const envChanges = (r.env_diff ?? []).filter(
    (p) => !/^\/(repo|tmp|root|proc|dev|run)/.test(p) && !p.includes("__pycache__"),
  );
  const own = r.steps_detail.filter((s) => !r.prefix || r.fork_step == null || s.step > r.fork_step);

  return (
    <>
      <Head
        crumbs={
          <>
            <a href="#/rollouts">Rollouts</a> / <a href={`#/task/${encodeURIComponent(r.task_id)}`}>{r.task_id}</a>
          </>
        }
        title={`Rollout #${r.id}`}
      />
      <Status tone={tone} label={label} why={why} />
      {flags.length ? (
        <Card title="Why it was flagged">
          <Table
            rows={flags}
            cols={[
              { cell: (f) => <Dot tone={f.severity === "high" ? "bad" : "warn"}>{human(f.rule)}</Dot> },
              { cell: (f) => <code>{f.evidence}</code>, className: "wrap" },
            ]}
          />
        </Card>
      ) : null}
      <Card title="Patch">
        <Diff patch={r.patch} />
      </Card>
      <Card title="Steps" extra={`${r.steps} · ${r.stop_reason ?? ""}`}>
        {(r.prefix ?? []).map((s) => (
          <StepRow key={`p${s.step}`} s={s} rolloutId={r.id} inherited />
        ))}
        {r.prefix ? <div className="marker">forked from #{r.parent_id}</div> : null}
        {own.map((s) => (
          <StepRow key={s.step} s={s} rolloutId={r.id} inherited={false} />
        ))}
        {!own.length && !r.prefix ? <Empty>No steps yet</Empty> : null}
      </Card>
      {failed.length ? (
        <Fold title="Failing tests" extra={failed.length}>
          <Code text={failed.join("\n")} />
        </Fold>
      ) : null}
      <Fold title="Graders">
        <Table
          rows={[
            { name: "Robust", how: "fresh env, patch only, per-test results", pass: r.robust_pass },
            { name: "Naive", how: "agent's own env, pytest exit code", pass: r.naive_pass },
          ]}
          cols={[
            { cell: (g) => g.name },
            { cell: (g) => <span className="soft">{g.how}</span> },
            { cell: (g) => <Pass v={g.pass} />, className: "r" },
          ]}
        />
        {r.naive_log ? <Code text={r.naive_log} /> : null}
      </Fold>
      {r.judge?.reason ? (
        <Fold title="LLM judge">
          <Pad>
            <span className="soft">{r.judge.reason}</span>
          </Pad>
        </Fold>
      ) : null}
      {envChanges.length ? (
        <Fold title="Changed outside repo" extra={envChanges.length}>
          <Code text={envChanges.join("\n")} />
        </Fold>
      ) : null}
      {r.tree.length > 1 ? (
        <Fold title="Related attempts" extra={r.tree.length}>
          <Pad>
            <Tree nodes={r.tree} root={r.tree_root} current={r.id} />
          </Pad>
        </Fold>
      ) : null}
    </>
  );
}

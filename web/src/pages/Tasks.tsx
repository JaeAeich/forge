import { send, type Task, type TaskRow } from "../api";
import { human, kindLabel, pct, plural, useApi } from "../lib";
import {
  Act,
  Status,
  Card,
  Code,
  Diff,
  type Filter,
  FilterList,
  Fold,
  Head,
  Loading,
  Pad,
  Pass,
  Pill,
  Result,
  Table,
} from "../ui";

const taskHref = (t: { id: string }) => `#/task/${encodeURIComponent(t.id)}`;

function TaskStatus({ t }: { t: TaskRow }) {
  if (t.status === "rejected")
    return (
      <span title={t.reject_reason ?? ""}>
        <Pill tone="bad">Rejected</Pill>
      </span>
    );
  if (t.solved) return <Pill tone="ok">{`Solved ${t.solved}/${t.rollouts}`}</Pill>;
  if (t.status === "valid") return <Pill>{t.rollouts ? `Unsolved 0/${t.rollouts}` : "Valid"}</Pill>;
  return <Pill tone="info">Validating</Pill>;
}

const FILTERS: Filter<TaskRow>[] = [
  ["valid", "Valid", (t) => t.status === "valid"],
  ["solved", "Solved", (t) => t.solved > 0],
  ["rejected", "Rejected", (t) => t.status === "rejected"],
  ["all", "All", () => true],
];

export function Tasks() {
  const { data, error } = useApi<TaskRow[]>("/tasks", 30000);
  if (!data) return <Loading error={error} />;
  return (
    <>
      <Head title="Tasks" />
      <Card>
        <FilterList
          id="tasks"
          rows={data}
          filters={FILTERS}
          search={(t) => `${t.id} ${t.title} ${t.reject_reason ?? ""}`}
          href={taskHref}
          empty="No tasks match"
          cols={[
            { head: "Task", cell: (t) => t.title, className: "wrap" },
            { head: "Repo", cell: (t) => <span className="soft">{t.repo}</span> },
            { head: "Status", cell: (t) => <TaskStatus t={t} />, className: "r" },
          ]}
        />
      </Card>
    </>
  );
}

export function TaskPage({ id }: { id: string }) {
  const { data: t, error } = useApi<Task>(`/tasks/${encodeURIComponent(id)}`);
  if (!t) return <Loading error={error} />;
  const valid = t.status === "valid";
  const solved = t.rollouts.filter((r) => r.robust_pass).length;
  const hacks = t.rollouts.filter((r) => r.verdict === "hack").length;
  const caughtHacks = hacks ? `${plural(hacks, "hack")} caught` : "";
  const exploits = t.hacks.filter((h) => h.is_hack);

  const status =
    t.status === "rejected" ? (
      <Status tone="bad" label="Rejected" why={t.reject_reason ?? ""} />
    ) : !valid ? (
      <Status tone="info" label="Validating" />
    ) : solved ? (
      <Status tone="ok" label={`Solved ${solved}/${t.rollouts.length}`} why={caughtHacks} />
    ) : (
      <Status
        tone=""
        label={t.rollouts.length ? `Unsolved 0/${t.rollouts.length}` : "Not attempted"}
        why={caughtHacks}
      />
    );

  return (
    <>
      <Head
        crumbs={
          <>
            <a href="#/tasks">Tasks</a> / {t.id}
          </>
        }
        title={t.title}
        actions={
          valid ? (
            <Act
              action="run-agent"
              run={() => send<{ rollout: number }>(`/tasks/${encodeURIComponent(t.id)}/rollout`, { kind: "agent" })}
              msg={(r) => `Queued rollout #${r.rollout}`}
            >
              Run agent
            </Act>
          ) : null
        }
      />
      {status}
      <Card title="Issue">
        <Code text={t.issue ?? t.commit_msg} lang="markdown" />
      </Card>
      {t.rollouts.length ? (
        <Card title="Rollouts">
          <Table
            rows={t.rollouts}
            href={(r) => `#/rollout/${r.id}`}
            cols={[
              { cell: (r) => `#${r.id}` },
              {
                cell: (r) => (
                  <>
                    {kindLabel(r)} {r.parent_id ? <span className="mut">fork of #{r.parent_id}</span> : null}
                  </>
                ),
              },
              { cell: (r) => <span className="soft">{r.steps} steps</span>, className: "r" },
              { cell: (r) => <Result r={r} />, className: "r" },
            ]}
          />
        </Card>
      ) : null}
      <Fold title="Reference fix">
        <Diff patch={t.gold_patch} />
      </Fold>
      <Fold title="Hidden tests" extra={plural(t.f2p?.length ?? 0, "failing test")}>
        <Diff patch={t.test_patch} />
      </Fold>
      <Fold
        title="Exploit tests"
        extra={exploits.length ? `${exploits.filter((h) => h.flagged).length}/${exploits.length} caught` : ""}
      >
        <Table
          rows={t.hacks}
          empty="Not run"
          cols={[
            {
              head: "Exploit",
              cell: (h) => (h.is_hack ? human(h.kind) : <span className="soft">{h.kind} (control)</span>),
            },
            { head: "Naive grader", cell: (h) => <Pass v={h.naive_pass} />, className: "r" },
            { head: "Robust grader", cell: (h) => <Pass v={h.robust_pass} />, className: "r" },
            { head: "Detected", cell: (h) => (h.is_hack ? <Pass v={h.flagged} /> : "–"), className: "r" },
          ]}
        />
      </Fold>
      <Fold title="Mutation tests" extra={t.mutation_score != null ? `${pct(t.mutation_score)} caught` : ""}>
        <Table
          rows={t.mutants ?? []}
          empty="Not run"
          cols={[
            { head: "Where", cell: (m) => <code>{`${m.file.split("/").pop()}:${m.line}`}</code> },
            { head: "Change", cell: (m) => human(m.op) },
            { head: "Caught", cell: (m) => <Pass v={m.killed} />, className: "r" },
          ]}
        />
      </Fold>
      <Fold title="Fairness" extra={t.judge?.solvable != null ? `${t.judge.solvable}/5` : ""}>
        <Pad>
          <span className="soft">{t.judge?.reason ?? "–"}</span>
        </Pad>
      </Fold>
    </>
  );
}

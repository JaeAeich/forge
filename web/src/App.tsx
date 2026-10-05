import { useEffect } from "react";
import { send, type Workers } from "./api";
import { plural, toastStore, useApi, useLiveUpdates, useRoute, useStore } from "./lib";
import { Bench, CodeIndex, Envs, Jobs, QA } from "./pages/More";
import { Overview } from "./pages/Overview";
import { RolloutPage, Rollouts } from "./pages/Rollouts";
import { TaskPage, Tasks } from "./pages/Tasks";
import { Help, helpOpen, MORE, PAGES, Palette, paletteOpen, useShortcuts } from "./palette";
import { Act, Dot, Empty } from "./ui";

function WorkerStatus() {
  const { data: w } = useApi<Workers>("/workers", 30000);
  if (!w) return null;
  if (!w.present)
    return (
      <span className="mut">
        Workers off · run <code>forge up</code>
      </span>
    );
  return (
    <>
      {w.paused ? (
        <Dot tone="warn">Paused</Dot>
      ) : w.active.length ? (
        <Dot tone="info">{`${plural(w.active.length, "job")} running on ${plural(w.count, "worker")}`}</Dot>
      ) : (
        <Dot tone="ok">{`${plural(w.count, "worker")} idle`}</Dot>
      )}
      <Act
        className="sm"
        run={() => send(`/workers/${w.paused ? "resume" : "pause"}`)}
        msg={w.paused ? "Resumed" : "Paused"}
      >
        {w.paused ? "Resume" : "Pause"}
      </Act>
    </>
  );
}

function Toast() {
  const t = useStore(toastStore);
  useEffect(() => {
    if (!t) return;
    const h = setTimeout(() => toastStore.set(null), 2500);
    return () => clearTimeout(h);
  }, [t]);
  return (
    <div className={`toast ${t ? "on" : ""}`} role="status" aria-live="polite">
      {t?.msg}
    </div>
  );
}

function Page({ view, arg }: { view: string; arg: string | undefined }) {
  switch (view) {
    case "overview":
      return <Overview />;
    case "tasks":
      return <Tasks />;
    case "task":
      return arg ? <TaskPage key={arg} id={arg} /> : null;
    case "rollouts":
      return <Rollouts />;
    case "rollout":
      return arg ? <RolloutPage key={arg} id={arg} /> : null;
    case "qa":
      return <QA />;
    case "index":
      return <CodeIndex />;
    case "envs":
      return <Envs />;
    case "jobs":
      return <Jobs />;
    case "bench":
      return <Bench />;
    default:
      return <Empty>Page not found</Empty>;
  }
}

export function App() {
  const { view, arg } = useRoute();
  useShortcuts(view, arg);
  useLiveUpdates();
  // biome-ignore lint/correctness/useExhaustiveDependencies: scroll to top whenever the route changes
  useEffect(() => {
    window.scrollTo(0, 0);
  }, [view, arg]);
  const active = view === "task" ? "tasks" : view === "rollout" ? "rollouts" : view;
  return (
    <>
      <header>
        <div className="in">
          <b>⚒ forge</b>
          <nav>
            {PAGES.map(([k, l]) => (
              <a key={k} href={`#/${k}`} className={k === active ? "on" : ""}>
                {l}
              </a>
            ))}
            <span className="sep" />
            {MORE.map(([k, l]) => (
              <a key={k} href={`#/${k}`} className={`minor ${k === active ? "on" : ""}`}>
                {l}
              </a>
            ))}
          </nav>
          <div className="right">
            <WorkerStatus />
            <button type="button" className="kbtn" title="Command palette" onClick={() => paletteOpen.set(true)}>
              <span className="kbd">⌘K</span>
            </button>
            <button type="button" className="kbtn" title="Shortcuts" onClick={() => helpOpen.set(true)}>
              <span className="kbd">?</span>
            </button>
          </div>
        </div>
      </header>
      <main>
        <Page view={view} arg={arg} />
      </main>
      <Palette view={view} arg={arg} />
      <Help />
      <Toast />
    </>
  );
}

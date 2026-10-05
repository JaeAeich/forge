import { type ReactNode, useEffect, useMemo, useRef, useState } from "react";
import { get, type RolloutRow, send, type TaskRow } from "./api";
import { act, go, listOrder, outcome, store, toast, useStore } from "./lib";
import { presetFilter } from "./ui";

export const PAGES: [string, string][] = [
  ["overview", "Overview"],
  ["tasks", "Tasks"],
  ["rollouts", "Rollouts"],
  ["qa", "QA"],
];
export const MORE: [string, string][] = [
  ["index", "Code index"],
  ["envs", "Environments"],
  ["jobs", "Jobs"],
  ["bench", "Benchmarks"],
];

export const paletteOpen = store(false);
export const helpOpen = store(false);

type Item = { group: string; label: string; hint?: string; key?: string; run: () => void };

function buildItems(view: string, arg: string | undefined, tasks: TaskRow[], rollouts: RolloutRow[]): Item[] {
  const items: Item[] = [...PAGES, ...MORE].map(([k, l]) => ({ group: "Page", label: l, run: () => go(`#/${k}`) }));
  if (view === "task" && arg) {
    const run = (kind: string) => () =>
      act(
        send<{ rollout: number }>(`/tasks/${encodeURIComponent(arg)}/rollout`, { kind }),
        (r) => `Queued rollout #${r.rollout}`,
      );
    items.unshift({ group: "Action", label: "Run red team on this task", run: run("redteam") });
    items.unshift({ group: "Action", label: "Run agent on this task", hint: "a", run: run("agent") });
  }
  items.push(
    {
      group: "Action",
      label: "Run pipeline on all repos",
      hint: "p",
      run: () => act(send("/pipeline/start"), "Pipeline queued"),
    },
    { group: "Action", label: "Pause workers", run: () => act(send("/workers/pause"), "Paused") },
    { group: "Action", label: "Resume workers", run: () => act(send("/workers/resume"), "Resumed") },
    {
      group: "Action",
      label: "Start a new environment stack",
      run: () => act(send<{ id: number }>("/envs"), (r) => `Stack #${r.id} started`),
    },
    {
      group: "Filter",
      label: "Solved rollouts",
      run: () => {
        presetFilter("rollouts", "solved");
        go("#/rollouts");
      },
    },
    {
      group: "Filter",
      label: "Hacks caught",
      run: () => {
        presetFilter("rollouts", "hack");
        go("#/rollouts");
      },
    },
    {
      group: "Filter",
      label: "Rejected tasks",
      run: () => {
        presetFilter("tasks", "rejected");
        go("#/tasks");
      },
    },
  );
  for (const t of tasks) {
    const state = t.status === "rejected" ? "rejected" : t.solved ? `solved ${t.solved}/${t.rollouts}` : t.status;
    items.push({
      group: "Task",
      label: t.title,
      hint: `${t.repo} · ${state}`,
      key: t.id,
      run: () => go(`#/task/${encodeURIComponent(t.id)}`),
    });
  }
  for (const r of rollouts) {
    const hint = outcome(r)[0] + (r.kind === "redteam" ? " · red team" : "");
    items.push({ group: "Rollout", label: `#${r.id} ${r.task_id}`, hint, run: () => go(`#/rollout/${r.id}`) });
  }
  return items;
}

/** All query words as substrings first; loose subsequence matches only when nothing matches exactly. */
function rank(items: Item[], query: string): Item[] {
  const q = query.trim().toLowerCase();
  if (!q) return items.filter((x) => x.group !== "Task" && x.group !== "Rollout");
  const hay = (x: Item) => `${x.group} ${x.label} ${x.hint ?? ""} ${x.key ?? ""}`.toLowerCase();
  const words = q.split(/\s+/);
  const groupBoost: Record<string, number> = { Page: 20, Action: 10 };
  let scored = items
    .filter((x) => words.every((w) => hay(x).includes(w)))
    .map((x) => {
      const l = x.label.toLowerCase();
      return {
        x,
        s: (l.startsWith(q) ? 100 : 0) + (l.includes(q) ? 50 : 0) + (groupBoost[x.group] ?? 0) - l.length * 0.01,
      };
    });
  if (!scored.length) {
    const flat = q.replace(/\s+/g, "");
    scored = items.flatMap((x) => {
      const h = hay(x);
      let i = 0;
      for (const c of flat) {
        i = h.indexOf(c, i) + 1;
        if (!i) return [];
      }
      return [{ x, s: -i }];
    });
  }
  return scored
    .sort((a, b) => b.s - a.s)
    .slice(0, 40)
    .map((o) => o.x);
}

const FOCUSABLE = "a[href],button:not(:disabled),input,textarea,select,summary,[tabindex]:not([tabindex='-1'])";

/** Modal overlay: dialog semantics, Esc closes, Tab stays inside, focus returns to where it was on close. */
function Modal(props: {
  label: string;
  className: string;
  onClose: () => void;
  anyClick?: boolean;
  children: ReactNode;
}) {
  const { label, className, onClose, anyClick, children } = props;
  const box = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const prev = document.activeElement as HTMLElement | null;
    box.current?.focus();
    return () => prev?.focus();
  }, []);
  const onKey = (e: React.KeyboardEvent) => {
    if (e.key === "Escape") {
      e.preventDefault();
      e.stopPropagation(); // the global Esc shortcut would also navigate back
      onClose();
    } else if (e.key === "Tab") {
      const els = [...(box.current?.querySelectorAll<HTMLElement>(FOCUSABLE) ?? [])];
      const first = els[0];
      const last = els[els.length - 1];
      if (!first || !last) e.preventDefault();
      else if (e.shiftKey && (document.activeElement === first || document.activeElement === box.current)) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && document.activeElement === last) {
        e.preventDefault();
        first.focus();
      }
    }
  };
  return (
    <div className="overlay" onClick={(e) => (anyClick || e.target === e.currentTarget) && onClose()}>
      <div
        ref={box}
        className={`box ${className}`}
        role="dialog"
        aria-modal="true"
        aria-label={label}
        tabIndex={-1}
        onKeyDown={onKey}
      >
        {children}
      </div>
    </div>
  );
}

type PaletteData = { tasks: TaskRow[]; rollouts: RolloutRow[] };
const CACHE_MS = 30000;
let cache: { at: number; data: PaletteData } | null = null;

export function Palette({ view, arg }: { view: string; arg: string | undefined }) {
  const open = useStore(paletteOpen);
  const [query, setQuery] = useState("");
  const [sel, setSel] = useState(0);
  const [data, setData] = useState<PaletteData>(() => cache?.data ?? { tasks: [], rollouts: [] });
  const input = useRef<HTMLInputElement>(null);
  const listRef = useRef<HTMLUListElement>(null);

  useEffect(() => {
    if (!open) return;
    setQuery("");
    setSel(0);
    input.current?.focus();
    if (cache && Date.now() - cache.at < CACHE_MS) {
      setData(cache.data);
      return;
    }
    Promise.all([get<TaskRow[]>("/tasks"), get<RolloutRow[]>("/rollouts")]).then(
      ([tasks, rollouts]) => {
        cache = { at: Date.now(), data: { tasks, rollouts } };
        setData(cache.data);
      },
      (e: Error) => toast(`Couldn't load tasks and rollouts: ${e.message}`),
    );
  }, [open]);

  const shown = useMemo(() => rank(buildItems(view, arg, data.tasks, data.rollouts), query), [view, arg, data, query]);

  useEffect(() => {
    listRef.current?.children[sel]?.scrollIntoView({ block: "nearest" });
  }, [sel]);

  if (!open) return null;
  const run = (i: number) => {
    const item = shown[i];
    paletteOpen.set(false);
    item?.run();
  };
  const onKey = (e: React.KeyboardEvent) => {
    if (e.key === "ArrowDown" || (e.ctrlKey && e.key === "n")) setSel((s) => Math.min(s + 1, shown.length - 1));
    else if (e.key === "ArrowUp" || (e.ctrlKey && e.key === "p")) setSel((s) => Math.max(s - 1, 0));
    else if (e.key === "Enter") run(sel);
    else return;
    e.preventDefault();
  };
  return (
    <Modal label="Command palette" className="" onClose={() => paletteOpen.set(false)}>
      <input
        ref={input}
        aria-label="Command palette"
        placeholder="Jump to a page, task, rollout, or run an action…"
        value={query}
        onChange={(e) => {
          setQuery(e.target.value);
          setSel(0);
        }}
        onKeyDown={onKey}
      />
      <ul ref={listRef} className="pal">
        {shown.map((x, i) => (
          <li
            key={`${x.group}${x.label}${x.key ?? ""}`}
            className={i === sel ? "on" : ""}
            onMouseMove={() => setSel(i)}
            onClick={() => run(i)}
          >
            <span className="g">{x.group}</span>
            <span>{x.label}</span>
            {x.hint ? <span className="h">{x.hint}</span> : null}
          </li>
        ))}
        {!shown.length ? <li className="mut">No matches</li> : null}
      </ul>
    </Modal>
  );
}

const K = ({ children }: { children: string }) => <span className="kbd">{children}</span>;

export function Help() {
  const open = useStore(helpOpen);
  if (!open) return null;
  const rows: [React.ReactNode, string][] = [
    [<K key="k">⌘K</K>, "Command palette: pages, tasks, rollouts, actions"],
    [
      <span key="g">
        <K>g</K> then <K>o t r q i e j b</K>
      </span>,
      "Go to Overview · Tasks · Rollouts · QA · Index · Envs · Jobs · Bench",
    ],
    [
      <span key="jk">
        <K>j</K> <K>k</K> <K>Enter</K>
      </span>,
      "Move through a list and open a row",
    ],
    [<K key="/">/</K>, "Search the current list"],
    [
      <span key="n">
        <K>1</K>–<K>6</K>
      </span>,
      "Switch list filter",
    ],
    [
      <span key="br">
        <K>[</K> <K>]</K>
      </span>,
      "Previous / next item on a detail page",
    ],
    [
      <span key="u">
        <K>u</K> <K>Esc</K>
      </span>,
      "Back to the list",
    ],
    [<K key="x">x</K>, "Expand / collapse every section"],
    [<K key="a">a</K>, "Run agent on this task"],
    [<K key="p">p</K>, "Run pipeline"],
    [<K key="?">?</K>, "This help"],
  ];
  return (
    <Modal label="Keyboard shortcuts" className="help" anyClick onClose={() => helpOpen.set(false)}>
      {rows.map(([k, d]) => (
        <div key={d} className="hrow">
          <span>{k}</span>
          <span className="soft">{d}</span>
        </div>
      ))}
    </Modal>
  );
}

// ---------- keyboard shortcuts ----------
const GO: Record<string, string> = {
  o: "overview",
  t: "tasks",
  r: "rollouts",
  q: "qa",
  i: "index",
  e: "envs",
  j: "jobs",
  b: "bench",
};
const emit = (name: string, detail?: number) => window.dispatchEvent(new CustomEvent(name, { detail }));

function sibling(view: string, arg: string | undefined, d: number) {
  const list = view === "task" ? "tasks" : view === "rollout" ? "rollouts" : null;
  if (!list || !arg) return;
  const order = listOrder[list] ?? [];
  const i = order.indexOf(`#/${view}/${encodeURIComponent(arg)}`);
  const next = i >= 0 ? order[i + d] : undefined;
  if (next) go(next);
  // not in the last list: guess by id; lists are newest first, so "next" is the lower id
  else if (i < 0 && view === "rollout") go(`#/rollout/${Math.max(1, Number(arg) - d)}`);
  else toast(d > 0 ? "Last item" : "First item");
}

export function useShortcuts(view: string, arg: string | undefined) {
  useEffect(() => {
    let gAt = 0;
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        paletteOpen.set(!paletteOpen.get());
        return;
      }
      // overlays handle their own keys; Esc closes them wherever focus is
      if (paletteOpen.get() || helpOpen.get()) {
        if (e.key === "Escape" || (e.key === "?" && helpOpen.get())) {
          e.preventDefault();
          paletteOpen.set(false);
          helpOpen.set(false);
        }
        return;
      }
      const target = e.target as HTMLElement;
      if (target.matches("input, textarea")) {
        if (e.key === "Escape") target.blur();
        if (e.key === "Enter" && target.id === "q") {
          target.blur();
          emit("forge:move", 1);
        }
        return;
      }
      if (e.metaKey || e.ctrlKey || e.altKey) return;
      const k = e.key;
      // let focused controls handle their own activation keys
      if ((k === "Enter" || k === " ") && target.closest("a,button,summary,select,[contenteditable]")) return;
      const dest = GO[k];
      if (Date.now() - gAt < 900 && dest) {
        gAt = 0;
        go(`#/${dest}`);
        return;
      }
      const actions: Record<string, () => void> = {
        g: () => {
          gAt = Date.now();
        },
        "?": () => helpOpen.set(true),
        "/": () => {
          const q = document.querySelector<HTMLInputElement>("#q, #sq");
          q?.focus();
          q?.select();
        },
        j: () => emit("forge:move", 1),
        ArrowDown: () => emit("forge:move", 1),
        k: () => emit("forge:move", -1),
        ArrowUp: () => emit("forge:move", -1),
        Enter: () => emit("forge:open"),
        "]": () => sibling(view, arg, 1),
        "[": () => sibling(view, arg, -1),
        u: () => (view === "task" ? go("#/tasks") : view === "rollout" ? go("#/rollouts") : undefined),
        Escape: () => (view === "task" ? go("#/tasks") : view === "rollout" ? go("#/rollouts") : undefined),
        x: () => {
          const ds = [...document.querySelectorAll<HTMLDetailsElement>("details.fold")];
          const openAll = !ds.every((d) => d.open);
          for (const d of ds) d.open = openAll;
        },
        p: () => void act(send("/pipeline/start"), "Pipeline queued"),
        a: () => document.querySelector<HTMLButtonElement>("[data-action=run-agent]")?.click(),
      };
      const fn = actions[k] ?? (/^[1-6]$/.test(k) ? () => emit("forge:filter", Number(k) - 1) : undefined);
      if (!fn) return;
      e.preventDefault();
      fn();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [view, arg]);
}

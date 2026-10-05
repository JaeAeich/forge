import { useCallback, useEffect, useRef, useState, useSyncExternalStore } from "react";
import { get, type Payload, type RolloutBrief } from "./api";

// ---------- formatting ----------
export const pct = (x: number | null | undefined) => (x == null ? "–" : `${Math.round(x * 100)}%`);
export const plural = (n: number, w: string) => `${n} ${w}${n === 1 ? "" : "s"}`;
export const human = (s: string) => s.replace(/_/g, " ");
export function ago(t: number | null | undefined): string {
  if (!t) return "–";
  const s = Date.now() / 1000 - t;
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

export type Tone = "" | "ok" | "bad" | "warn" | "info";

export function outcome(r: Pick<RolloutBrief, "status" | "verdict" | "robust_pass">): [string, Tone] {
  if (r.status === "queued") return ["Queued", ""];
  if (r.status === "running") return ["Running", "info"];
  if (r.status === "done") return ["Grading", "info"];
  if (r.verdict === "hack") return ["Hack caught", "bad"];
  if (r.robust_pass) return ["Solved", "ok"];
  if (r.verdict === "suspect") return ["Not solved · suspect", "warn"];
  return ["Not solved", ""];
}

export const kindLabel = (r: { kind: string }) => (r.kind === "redteam" ? "red team" : "agent");

export const JOB_LABEL: Record<string, string> = {
  mine_repo: "Mine",
  index_repo: "Index",
  validate_task: "Validate",
  rollout: "Rollout",
  grade: "Grade",
  hack_corpus: "Exploit tests",
  mutate: "Mutation tests",
};
export const jobTarget = (p: Payload) => p.task ?? p.repo ?? (p.rollout ? `rollout #${p.rollout}` : "");
export const jobHref = (p: Payload) =>
  p.task ? `#/task/${encodeURIComponent(p.task)}` : p.rollout ? `#/rollout/${p.rollout}` : undefined;

// ---------- routing ----------
const subscribeHash = (cb: () => void) => {
  window.addEventListener("hashchange", cb);
  return () => window.removeEventListener("hashchange", cb);
};
export function useRoute(): { view: string; arg: string | undefined } {
  const hash = useSyncExternalStore(subscribeHash, () => window.location.hash);
  const [view = "overview", arg] = (hash.slice(2) || "overview").split("/");
  return { view, arg: arg === undefined ? undefined : decodeURIComponent(arg) };
}
export const go = (href: string) => {
  window.location.hash = href;
};

// ---------- data ----------
/** Fetch JSON from the API, optionally re-polling. `reload()` refetches immediately; only the latest response lands. */
export function useApi<T>(path: string | null, pollMs = 0) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const seq = useRef(0);

  const reload = useCallback(async () => {
    if (!path) return;
    const id = ++seq.current;
    try {
      const d = await get<T>(path);
      if (id !== seq.current) return;
      setData(d);
      setError(null);
    } catch (e) {
      if (id === seq.current) setError((e as Error).message);
    }
  }, [path]);

  // refetch after actions/live events, but not on mount (the effect below already fetches)
  const tick = useStore(refreshStore);
  const seen = useRef(tick);
  useEffect(() => {
    if (tick === seen.current) return;
    seen.current = tick;
    void reload();
  }, [tick, reload]);

  useEffect(() => {
    seq.current++;
    setData(null);
    setError(null);
    void reload();
    if (!pollMs) return;
    const h = setInterval(() => {
      if (document.visibilityState === "visible") void reload();
    }, pollMs);
    return () => clearInterval(h);
  }, [reload, pollMs]);

  return { data, error, reload };
}

// ---------- tiny global stores ----------
/** Ordered row links of the last keyboard-navigable list, used by [ and ] on detail pages. */
export const listOrder: Record<string, string[]> = {};

type Listener = () => void;
export type Store<T> = { get: () => T; set: (v: T) => void; subscribe: (cb: Listener) => () => void };
export function store<T>(initial: T): Store<T> {
  let value = initial;
  const subs = new Set<Listener>();
  return {
    get: () => value,
    set(v) {
      value = v;
      for (const s of subs) s();
    },
    subscribe(cb) {
      subs.add(cb);
      return () => subs.delete(cb);
    },
  };
}
export const useStore = <T>(s: Store<T>): T => useSyncExternalStore(s.subscribe, s.get);

export const toastStore = store<{ msg: string; at: number } | null>(null);
export const toast = (msg: string) => toastStore.set({ msg, at: Date.now() });
/** Bumped after every action so open pages refetch. */
export const refreshStore = store(0);
const bump = () => refreshStore.set(refreshStore.get() + 1);

/** Live updates: the server relays NATS events over SSE; any event triggers a (throttled) refetch. */
export function useLiveUpdates() {
  useEffect(() => {
    let timer: ReturnType<typeof setTimeout> | undefined;
    const source = new EventSource("/api/events");
    source.onmessage = () => {
      timer ??= setTimeout(() => {
        timer = undefined;
        bump();
      }, 750);
    };
    return () => {
      source.close();
      clearTimeout(timer);
    };
  }, []);
}

export async function act<T>(p: Promise<T>, msg: string | ((r: T) => string)) {
  try {
    const r = await p;
    toast(typeof msg === "function" ? msg(r) : msg);
    bump();
    return r;
  } catch (e) {
    toast(`Error: ${(e as Error).message}`);
    return null;
  }
}

/** `act` with an in-flight guard: `busy` disables the button, the ref drops double-clicks before re-render. */
export function useAct() {
  const [busy, setBusy] = useState(false);
  const lock = useRef(false);
  const run = useCallback(async <T>(fn: () => Promise<T>, msg: string | ((r: T) => string)) => {
    if (lock.current) return null;
    lock.current = true;
    setBusy(true);
    try {
      return await act(fn(), msg);
    } finally {
      lock.current = false;
      setBusy(false);
    }
  }, []);
  return [busy, run] as const;
}

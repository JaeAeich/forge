import hljs from "highlight.js/lib/common";
import { type MouseEvent, type ReactNode, useEffect, useMemo, useRef, useState } from "react";
import { go, listOrder, outcome, type Tone, useAct } from "./lib";

export const Pill = ({ tone = "", children }: { tone?: Tone; children: ReactNode }) => (
  <span className={`pill ${tone}`}>{children}</span>
);
export const Dot = ({ tone = "", children }: { tone?: Tone; children: ReactNode }) => (
  <span className={`dot ${tone}`}>{children}</span>
);
export const Pass = ({ v }: { v: number | boolean | null | undefined }) =>
  v == null ? <span className="mut">–</span> : v ? <span className="yes">✓</span> : <span className="no">✗</span>;

export function Result({ r }: { r: Parameters<typeof outcome>[0] }) {
  const [label, tone] = outcome(r);
  return <Pill tone={tone}>{label}</Pill>;
}

/** One-line outcome under a page title: a status pill and, if there is one, the reason. */
export const Status = ({ tone, label, why }: { tone: Tone; label: string; why?: string | undefined }) => (
  <div className="status">
    <Pill tone={tone}>{label}</Pill>
    {why ? <span className="why">{why}</span> : null}
  </div>
);

export function Head({ crumbs, title, actions }: { crumbs?: ReactNode; title: ReactNode; actions?: ReactNode }) {
  return (
    <>
      {crumbs ? <div className="crumbs">{crumbs}</div> : null}
      <div className="head">
        <h1>{title}</h1>
        {actions ? <div className="act">{actions}</div> : null}
      </div>
    </>
  );
}

export function Card({ title, extra, children }: { title?: ReactNode; extra?: ReactNode; children: ReactNode }) {
  return (
    <div className="card">
      {title ? (
        <div className="hd">
          <h3>{title}</h3>
          {extra != null && extra !== "" ? <span className="x">{extra}</span> : null}
        </div>
      ) : null}
      {children}
    </div>
  );
}

/** Secondary information: collapsed by default. `x` toggles every Fold on the page. */
export function Fold({ title, extra, children }: { title: string; extra?: ReactNode; children: ReactNode }) {
  return (
    <details className="card fold">
      <summary className="hd">
        <h3>{title}</h3>
        {extra != null && extra !== "" ? <span className="x">{extra}</span> : null}
      </summary>
      {children}
    </details>
  );
}

export const Pad = ({ children }: { children: ReactNode }) => <div className="bd">{children}</div>;
/** Placeholder while `useApi` has no data; shows the fetch error instead of spinning forever. */
export const Loading = ({ error }: { error?: string | null }) => (
  <div className="empty">{error ? `Couldn't load: ${error}` : "Loading…"}</div>
);
export const Empty = ({ children }: { children: ReactNode }) => <div className="empty">{children}</div>;

export function Stat(props: { label: string; value: ReactNode; sub?: string; href?: string }) {
  const { label, value, sub, href } = props;
  const body = (
    <>
      <div className="l">{label}</div>
      <div className="n">{value}</div>
      <div className="s">{sub || " "}</div>
    </>
  );
  return href ? (
    <a className="stat link" href={href}>
      {body}
    </a>
  ) : (
    <div className="stat">{body}</div>
  );
}

/** Button that runs a mutating API call via `act`, disabled while in flight. */
export function Act<T>(props: {
  run: () => Promise<T>;
  msg: string | ((r: T) => string);
  confirm?: string;
  className?: string;
  title?: string;
  action?: string;
  children: ReactNode;
}) {
  const { run, msg, confirm: ask, className, title, action, children } = props;
  const [busy, start] = useAct();
  const onClick = (e: MouseEvent) => {
    e.preventDefault(); // buttons inside <summary> must not toggle it
    if (!ask || confirm(ask)) void start(run, msg);
  };
  return (
    <button type="button" className={className} title={title} data-action={action} disabled={busy} onClick={onClick}>
      {children}
    </button>
  );
}

// ---------- tables ----------
export type Col<T> = { head?: string; cell: (row: T) => ReactNode; className?: string };

/** Table whose rows can be links. With `keyboard`, j/k/Enter select and open rows (see shortcuts.ts). */
// Generic tables have no stable row ids; rows are fully re-rendered from fresh data, so index keys are fine here.
export function Table<T>(props: {
  rows: T[];
  cols: Col<T>[];
  href?: (row: T) => string | undefined;
  empty?: string;
  keyboard?: string;
}) {
  const { rows, cols, href, empty = "Nothing here yet", keyboard } = props;
  const [sel, setSel] = useState(-1);
  const selRef = useRef(sel);
  selRef.current = sel;

  useEffect(() => {
    if (!keyboard || !href) return;
    listOrder[keyboard] = rows.map(href).filter((h): h is string => !!h);
    const onMove = (e: Event) => {
      const d = (e as CustomEvent<number>).detail;
      setSel((s) => Math.max(0, Math.min(rows.length - 1, s + d)));
    };
    const onOpen = () => {
      const row = rows[selRef.current];
      const to = row && href(row);
      if (to) go(to);
    };
    window.addEventListener("forge:move", onMove);
    window.addEventListener("forge:open", onOpen);
    return () => {
      window.removeEventListener("forge:move", onMove);
      window.removeEventListener("forge:open", onOpen);
    };
  }, [keyboard, href, rows]);

  useEffect(() => {
    if (sel >= 0) document.querySelector("tr.sel")?.scrollIntoView({ block: "nearest" });
  }, [sel]);

  if (!rows.length) return <Empty>{empty}</Empty>;
  return (
    <table>
      {cols.some((c) => c.head) ? (
        <thead>
          <tr>
            {cols.map((c, i) => (
              <th key={i} className={c.className}>
                {c.head}
              </th>
            ))}
          </tr>
        </thead>
      ) : null}
      <tbody>
        {rows.map((row, i) => {
          const to = href?.(row);
          return (
            <tr
              key={i}
              className={`${to ? "link" : ""} ${i === sel ? "sel" : ""}`}
              onClick={to ? (e) => !(e.target as HTMLElement).closest("a,button") && go(to) : undefined}
            >
              {cols.map((c, j) => (
                <td key={j} className={c.className}>
                  {c.cell(row)}
                </td>
              ))}
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}

export type Filter<T> = [key: string, label: string, test: (row: T) => boolean];

/** Filter pills + search box above a keyboard-navigable table. State survives re-renders and polling. */
const listState: Record<string, { f: string; q: string }> = {};
export function FilterList<T>(props: {
  id: string;
  rows: T[];
  filters: Filter<T>[];
  search: (row: T) => string;
  cols: Col<T>[];
  href?: (row: T) => string | undefined;
  empty?: string;
}) {
  const { id, rows, filters, search, cols, href, empty } = props;
  const init = listState[id] ?? { f: filters[0]?.[0] ?? "", q: "" };
  const [f, setF] = useState(init.f);
  const [q, setQ] = useState(init.q);
  listState[id] = { f, q };

  useEffect(() => {
    const onFilter = (e: Event) => {
      const i = (e as CustomEvent<number>).detail;
      const key = filters[i]?.[0];
      if (key) setF(key);
    };
    const onPreset = (e: Event) => {
      const p = (e as CustomEvent<{ list: string; f: string }>).detail;
      if (p.list !== id) return;
      setF(p.f);
      setQ("");
    };
    window.addEventListener("forge:filter", onFilter);
    window.addEventListener("forge:preset", onPreset);
    return () => {
      window.removeEventListener("forge:filter", onFilter);
      window.removeEventListener("forge:preset", onPreset);
    };
  }, [filters, id]);

  const shown = useMemo(() => {
    const test = filters.find((x) => x[0] === f)?.[2] ?? (() => true);
    const needle = q.toLowerCase();
    return rows.filter(test).filter((r) => !needle || search(r).toLowerCase().includes(needle));
  }, [rows, filters, f, q, search]);

  return (
    <>
      <div className="filters">
        {filters.map(([key, label, test]) => (
          <button
            type="button"
            key={key}
            className={`f ${key === f ? "on" : ""}`}
            aria-pressed={key === f}
            onClick={() => setF(key)}
          >
            {label}
            <span className="c">{rows.filter(test).length}</span>
          </button>
        ))}
        <input id="q" placeholder="Search" aria-label="Search list" value={q} onChange={(e) => setQ(e.target.value)} />
      </div>
      {/* keyed so the keyboard selection resets whenever the filter or search changes */}
      <Table key={`${f}|${q}`} rows={shown} cols={cols} href={href} empty={empty} keyboard={id} />
    </>
  );
}

/** Lets other modules (command palette) preset a list filter, whether or not that list is mounted. */
export const presetFilter = (list: string, f: string) => {
  listState[list] = { f, q: "" };
  window.dispatchEvent(new CustomEvent("forge:preset", { detail: { list, f } }));
};

// ---------- code ----------
const highlight = (text: string, lang: string) =>
  hljs.getLanguage(lang) ? hljs.highlight(text, { language: lang, ignoreIllegals: true }).value : escapeHtml(text);
const escapeHtml = (s: string) =>
  s.replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c] ?? c);

export function Code({ text, lang = "plaintext" }: { text: string | null | undefined; lang?: string }) {
  const html = useMemo(() => highlight(text ?? "", lang), [text, lang]);
  return (
    <pre>
      {/* biome-ignore lint/security/noDangerouslySetInnerHtml: highlight.js output is escaped */}
      <code className="hljs" dangerouslySetInnerHTML={{ __html: html }} />
    </pre>
  );
}

/** Unified diff rendered GitHub-style: file headers, hunk markers, tinted +/- lines, syntax highlighting. */
export function Diff({ patch }: { patch: string | null | undefined }) {
  const lines = useMemo(() => {
    const out: { kind: "file" | "hunk" | "add" | "del" | "ctx"; html: string }[] = [];
    let binary = false;
    for (const l of (patch ?? "").split("\n")) {
      if (l.startsWith("diff --git")) {
        binary = false;
        out.push({ kind: "file", html: escapeHtml(l.split(" b/").pop() ?? "") });
      } else if (l.startsWith("GIT binary patch") || l.startsWith("Binary files")) {
        binary = true;
        out.push({ kind: "hunk", html: "binary file, contents hidden" });
      } else if (binary || /^(index |--- |\+\+\+ |new file|deleted file)/.test(l)) continue;
      else if (l.startsWith("@@")) out.push({ kind: "hunk", html: escapeHtml(l.replace(/^(@@[^@]*@@).*/, "$1")) });
      else {
        const kind = l[0] === "+" ? "add" : l[0] === "-" ? "del" : "ctx";
        out.push({ kind, html: `${kind === "ctx" ? " " : l[0]} ${highlight(l.slice(1), "python")}` });
      }
    }
    return out;
  }, [patch]);
  if (!patch?.trim()) return <Empty>No changes</Empty>;
  return (
    <pre className="diff">
      <code className="hljs">
        {lines.map((l, i) => (
          // biome-ignore lint/security/noDangerouslySetInnerHtml: escaped above
          <span key={i} className={`ln ${l.kind}`} dangerouslySetInnerHTML={{ __html: l.html }} />
        ))}
      </code>
    </pre>
  );
}

"use client";

import { useEffect, useId, useMemo, useRef, useState } from "react";
import type { KeyboardEvent as ReactKeyboardEvent } from "react";

import { useRouter } from "next/navigation";

import { agentLabel } from "@/components/dashboard/format";
import { NAV_ITEMS } from "@/components/layout/nav-items";
import { errorText } from "@/components/tasks/status";
import { cn } from "@/components/ui";
import { api } from "@/lib/api";

/**
 * The Ctrl+K command palette (ui-gap §11) — one overlay that searches and navigates the SHIPPED
 * product: the fourteen routes (`components/layout/nav-items.ts` — the shared table is the
 * count), tasks, runs and reports, all through deep links that already exist.
 *
 * Purely client-side: no new endpoint, no new backend call shape. Three lazy READS (the same
 * list endpoints the pages themselves consume) run on the FIRST open and are reused for the
 * session — closing and reopening renders from what was already read. A read that fails renders
 * ONE inline honest note line (the failure's own message) inside its section; the section stays
 * visible. Nothing here invents vocabulary: routes come from the shared `NAV_ITEMS` table, task
 * rows navigate to the tasks page's existing `?task=<id>` deep link, run rows to the /usage
 * ledger, report rows to the dashboard.
 *
 * Modal grammar mirrors `TaskDetailSheet` (the app's ONE measured sheet grammar,
 * sheets-and-composer.md §1): full-viewport scrim, `role="dialog"` + `aria-modal`, focus moves
 * in on open and RETURNS to the previously-focused element on close, Esc closes, scrim press
 * closes. Two deliberate differences, both from the palette's spec: the scrim press IS a close
 * gesture here (the sheet's OPEN DECISION #2 refused it for a data view; a transient search
 * overlay is the other case), and Escape is handled on `window` while open so it works even if
 * focus ever slipped out from under the input.
 *
 * A11y pattern (ONE coherent choice): the results container is `role="listbox"` with
 * `role="option"` rows grouped per section (`role="group"` labelled by the mono-caps header),
 * and the filter input is a `role="combobox"` carrying `aria-activedescendant` — the standard
 * APG combobox/listbox pattern. DOM focus never leaves the input; ArrowDown/ArrowUp move the
 * ACTIVE option, Enter opens it. That is why the arrow/Enter/Tab handling lives on the dialog
 * (the input cannot lose focus to a row) and why options are divs, not buttons.
 */

// The scrim TaskDetailSheet carries (sheets-and-composer.md §1): ≈55% black, the one measured
// overlay value in this app; a local constant here exactly as it is there, no new colour.
const SCRIM = "rgba(0, 0, 0, 0.55)";

/** The three lazy reads, pinned to the shipped list endpoints. */
const TASKS_READ = "/tasks?limit=50";
const RUNS_READ = "/runs?limit=50";
const REPORTS_READ = "/dashboard/reports?limit=20";

/** Rendered rows per section before the honest "…and N more" line — never a silent cut. */
const SECTION_CAP = 8;

/** The fields each section actually renders, off the real wire rows. */
type TaskPaletteRow = { id: number; title: string };
type RunPaletteRow = { id: number; task_title: string | null; agent_type: string };
type ReportPaletteRow = { id: number; report_date: string };

type ReadState<T> =
  | { status: "loading" }
  | { status: "ready"; rows: T[] }
  | { status: "error"; message: string };

type PaletteItem = { key: string; label: string; hint: string; href: string };

type SectionView = {
  id: "routes" | "tasks" | "runs" | "reports";
  title: string;
  state: "static" | "loading" | "ready" | "error";
  /** The section's one state line: a failure message, "Loading…", or the empty read's own line. */
  note: { tone: "muted" | "alert"; text: string } | null;
  /** Every FILTERED item, in display order (the cap is applied at render time). */
  items: PaletteItem[];
};

/**
 * The row list out of a read body. `GET /tasks` answers a bare JSON list; `GET /runs` and
 * `GET /dashboard/reports` answer `{items, total}` wrappers. BOTH shapes are accepted for each
 * read so a server that serves the other one still renders its REAL rows — and anything else
 * throws, whose message becomes the section's honest note line (never a silently-empty list).
 */
function rowsOf(body: unknown, what: string): unknown[] {
  if (Array.isArray(body)) return body;
  if (body !== null && typeof body === "object" && Array.isArray((body as { items?: unknown }).items)) {
    return (body as { items: unknown[] }).items;
  }
  throw new Error(`the server returned no ${what} list`);
}

/**
 * A run row's label off its real fields: its task title when the ledger recorded one, else the
 * agent label the run does carry plus its ledger id — never a blank row, never a fabricated one.
 */
function runLabel(row: RunPaletteRow): string {
  const title = typeof row.task_title === "string" ? row.task_title.trim() : "";
  if (title !== "") return title;
  const agent = typeof row.agent_type === "string" ? row.agent_type : "";
  return `${agentLabel(agent)} run #${row.id}`.trim();
}

export function CommandPalette() {
  const router = useRouter();
  const baseId = useId();
  const listboxId = `${baseId}-listbox`;

  const dialogRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  /** The element focused when the palette opened — focus returns to it on close (the sheet's B17). */
  const openerRef = useRef<HTMLElement | null>(null);
  /** The lazy reads fire on the FIRST open only, then are reused for the session. */
  const loadedRef = useRef(false);

  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  /** Index into the FLATTENED visible list; -1 = nothing highlighted yet. */
  const [highlight, setHighlight] = useState(-1);

  const [tasksRead, setTasksRead] = useState<ReadState<TaskPaletteRow>>({ status: "loading" });
  const [runsRead, setRunsRead] = useState<ReadState<RunPaletteRow>>({ status: "loading" });
  const [reportsRead, setReportsRead] = useState<ReadState<ReportPaletteRow>>({ status: "loading" });

  /**
   * The global combo, live for the whole mount: Ctrl+K / Cmd+K toggles (both are preventDefaulted
   * so the browser's own Ctrl+K never fires), and Esc closes while open — window-level so it
   * closes even if focus ever escaped the input. Cleanup on unmount.
   */
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (
        (event.ctrlKey || event.metaKey) &&
        !event.altKey &&
        !event.shiftKey &&
        event.key.toLowerCase() === "k"
      ) {
        event.preventDefault();
        setOpen((current) => !current);
        return;
      }
      if (open && event.key === "Escape") {
        event.preventDefault();
        setOpen(false);
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [open]);

  /**
   * Open → remember what had focus (BEFORE we move it), start a clean query, focus the filter
   * input. Close (or unmount while open) → hand focus back. No `autoFocus` attribute on the
   * input: it would focus during mount, before this capture, and the capture would record the
   * palette itself instead of the opener.
   */
  useEffect(() => {
    if (!open) return;
    openerRef.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    setQuery("");
    setHighlight(-1);
    inputRef.current?.focus();
    return () => {
      openerRef.current?.focus();
    };
  }, [open]);

  /** The three lazy reads: first open only; reused for the session (successes AND failures). */
  useEffect(() => {
    if (!open || loadedRef.current) return;
    loadedRef.current = true;

    api
      .get<unknown>(TASKS_READ)
      .then((body) => setTasksRead({ status: "ready", rows: rowsOf(body, "tasks") as TaskPaletteRow[] }))
      .catch((e: unknown) => setTasksRead({ status: "error", message: errorText(e) }));

    api
      .get<unknown>(RUNS_READ)
      .then((body) => setRunsRead({ status: "ready", rows: rowsOf(body, "runs") as RunPaletteRow[] }))
      .catch((e: unknown) => setRunsRead({ status: "error", message: errorText(e) }));

    api
      .get<unknown>(REPORTS_READ)
      .then((body) =>
        setReportsRead({ status: "ready", rows: rowsOf(body, "reports") as ReportPaletteRow[] }),
      )
      .catch((e: unknown) => setReportsRead({ status: "error", message: errorText(e) }));
  }, [open]);

  const trimmedQuery = query.trim().toLowerCase();

  /** The four sections in pinned order, filtered by the query (case-insensitive over the label). */
  const sections = useMemo<SectionView[]>(() => {
    const matches = (label: string) => trimmedQuery === "" || label.toLowerCase().includes(trimmedQuery);

    const list: SectionView[] = [
      {
        id: "routes",
        title: "ROUTES",
        state: "static",
        note: null,
        items: NAV_ITEMS.filter((item) => matches(item.label)).map((item) => ({
          key: `route:${item.href}`,
          label: item.label,
          hint: item.href,
          href: item.href,
        })),
      },
    ];

    const pushRead = <T,>(
      id: "tasks" | "runs" | "reports",
      title: string,
      state: ReadState<T>,
      toItem: (row: T) => PaletteItem,
      what: string,
    ) => {
      if (state.status === "error") {
        list.push({ id, title, state: "error", note: { tone: "alert", text: state.message }, items: [] });
        return;
      }
      if (state.status === "loading") {
        list.push({ id, title, state: "loading", note: { tone: "muted", text: "Loading…" }, items: [] });
        return;
      }
      list.push({
        id,
        title,
        state: "ready",
        // An honest empty line only when the read itself found nothing — a query that matches
        // nothing in a populated section is not "no tasks recorded".
        note:
          state.rows.length === 0 && trimmedQuery === ""
            ? { tone: "muted", text: `No ${what} recorded.` }
            : null,
        items: state.rows.map(toItem).filter((item) => matches(item.label)),
      });
    };

    pushRead(
      "tasks",
      "TASKS",
      tasksRead,
      (row) => ({
        key: `task:${row.id}`,
        label: typeof row.title === "string" ? row.title : `task #${row.id}`,
        // The route the tasks page already serves: `?task=<id>` opens that task's sheet.
        hint: "TASKS",
        href: `/tasks?task=${row.id}`,
      }),
      "tasks",
    );
    pushRead(
      "runs",
      "RUNS",
      runsRead,
      (row) => ({ key: `run:${row.id}`, label: runLabel(row), hint: "RUNS", href: "/usage" }),
      "runs",
    );
    pushRead(
      "reports",
      "REPORTS",
      reportsRead,
      (row) => ({
        key: `report:${row.id}`,
        label: typeof row.report_date === "string" ? row.report_date : `report #${row.id}`,
        hint: "REPORTS",
        href: "/dashboard",
      }),
      "reports",
    );

    return list;
  }, [trimmedQuery, tasksRead, runsRead, reportsRead]);

  /** Apply the per-section cap and build the FLATTENED visible list the arrows move over.
   *  The ROUTES table is the fixed fourteen-route IA (`components/layout/nav-items.ts` — the
   *  table itself is the count; this comment drifted at "ten" between V11's three route
   *  additions and the V15/W2 P2-02 correction) and is exempt: the acceptance ("lists all route
   *  labels") wins over a cap that would hide two navigable pages behind an "…and 2 more" line.
   *  The cap is for the read sections, whose length is unbounded. */
  const model = useMemo(() => {
    const flat: Array<PaletteItem & { flatIndex: number }> = [];
    const rendered = sections.map((section) => {
      const cap = section.id === "routes" ? section.items.length : SECTION_CAP;
      const visible = section.items.slice(0, cap).map((item) => {
        const entry = { ...item, flatIndex: flat.length };
        flat.push(entry);
        return entry;
      });
      // The more-line counts what THIS section's cap actually hid — using a per-section cap: for
      // ROUTES (cap = length, nothing hidden) a false "…and N more" was the parent-gate defect.
      return { ...section, visible, hiddenCount: Math.max(0, section.items.length - cap) };
    });
    return { rendered, flat };
  }, [sections]);

  const activeItem = highlight >= 0 && highlight < model.flat.length ? model.flat[highlight] : null;
  const activeOptionId = activeItem ? `${baseId}-opt-${activeItem.flatIndex}` : undefined;

  /**
   * A query with nowhere to go: the single "No matches." line. Suppressed while a read is still
   * loading or in error — the unread section might yet contain matches, and claiming none would
   * be a claim we cannot stand behind (the state notes speak instead).
   */
  const showNoMatches =
    trimmedQuery !== "" &&
    model.flat.length === 0 &&
    !sections.some((section) => section.state === "error" || section.state === "loading");

  const navigate = (href: string) => {
    setOpen(false);
    router.push(href);
  };

  /** Tab is trapped: with the input as the dialog's only focusable, focus wraps onto itself. */
  const trapTab = (shiftKey: boolean) => {
    const root = dialogRef.current;
    if (!root) return;
    const focusables = Array.from(
      root.querySelectorAll<HTMLElement>(
        'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
      ),
    );
    if (focusables.length === 0) return;
    const first = focusables[0];
    const last = focusables[focusables.length - 1];
    const active = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    if (shiftKey) {
      if (active === first || !root.contains(active)) last.focus();
    } else if (active === last || !root.contains(active)) {
      first.focus();
    }
  };

  const onDialogKeyDown = (event: ReactKeyboardEvent<HTMLDivElement>) => {
    const flat = model.flat;
    if (event.key === "Tab") {
      event.preventDefault();
      trapTab(event.shiftKey);
      return;
    }
    if (event.key === "ArrowDown") {
      event.preventDefault();
      if (flat.length === 0) return;
      setHighlight((current) => (current < 0 ? 0 : (current + 1) % flat.length));
      return;
    }
    if (event.key === "ArrowUp") {
      event.preventDefault();
      if (flat.length === 0) return;
      setHighlight((current) => (current < 0 ? flat.length - 1 : (current - 1 + flat.length) % flat.length));
      return;
    }
    if (event.key === "Enter") {
      event.preventDefault();
      if (activeItem) navigate(activeItem.href);
    }
  };

  // Mounted globally, the palette renders NOTHING until opened.
  if (!open) return null;

  return (
    <div
      data-testid="palette-scrim"
      className="fixed inset-0 z-50 flex items-start justify-center overflow-y-auto p-4"
      style={{ backgroundColor: SCRIM }}
      onClick={(event) => {
        // A scrim press closes; a press inside the panel must not (the sheet's scrim guard).
        if (event.target === event.currentTarget) setOpen(false);
      }}
    >
      <div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-label="Command palette"
        onKeyDown={onDialogKeyDown}
        className="card-surface my-4 w-full max-w-[637px] shadow-2xl"
      >
        <div className="p-2">
          <input
            ref={inputRef}
            role="combobox"
            aria-expanded="true"
            aria-controls={listboxId}
            aria-activedescendant={activeOptionId}
            aria-autocomplete="list"
            aria-label="Command palette search"
            autoComplete="off"
            placeholder="Search tasks, runs, reports…"
            value={query}
            onChange={(event) => {
              setQuery(event.target.value);
              setHighlight(-1);
            }}
            className="h-[49px] w-full border border-line-strong bg-surface px-3 text-sm text-ink"
          />
        </div>

        <div
          id={listboxId}
          role="listbox"
          aria-label="Command palette results"
          className="max-h-[70vh] overflow-y-auto border-t border-line px-2 py-2"
        >
          {showNoMatches ? (
            <p className="px-2 py-1 text-sm text-ink-muted">No matches.</p>
          ) : (
            model.rendered
              .filter(
                (section) =>
                  trimmedQuery === "" ||
                  section.visible.length > 0 ||
                  section.state === "error" ||
                  section.state === "loading",
              )
              .map((section) => (
                <section
                  key={section.id}
                  role="group"
                  aria-labelledby={`${baseId}-${section.id}`}
                  className="mb-2 last:mb-0"
                >
                  <p id={`${baseId}-${section.id}`} className="ui-label px-2 py-1 text-ink-muted">
                    {section.title}
                  </p>
                  {section.note ? (
                    <p
                      role={section.note.tone === "alert" ? "alert" : undefined}
                      className={cn(
                        "px-2 py-1 text-xs",
                        section.note.tone === "alert" ? "text-alert" : "text-ink-muted",
                      )}
                    >
                      {section.note.text}
                    </p>
                  ) : null}
                  {section.visible.map((item) => (
                    <div
                      key={item.key}
                      id={`${baseId}-opt-${item.flatIndex}`}
                      role="option"
                      aria-selected={item.flatIndex === highlight}
                      className={cn(
                        "flex cursor-pointer items-center gap-3 px-2 py-1.5 text-sm text-ink",
                        item.flatIndex === highlight && "bg-surface-sunk",
                      )}
                      onClick={() => navigate(item.href)}
                    >
                      <span className="truncate">{item.label}</span>
                      <span className="ml-auto shrink-0 font-mono text-xs text-ink-muted">{item.hint}</span>
                    </div>
                  ))}
                  {section.hiddenCount > 0 ? (
                    <p className="px-2 py-1 text-xs text-ink-muted">{`…and ${section.hiddenCount} more`}</p>
                  ) : null}
                </section>
              ))
          )}
        </div>
      </div>
    </div>
  );
}

"use client";

import { useEffect, useId, useRef, useState } from "react";
import type { KeyboardEvent } from "react";

import { Button, Tabs, type TabItem } from "@/components/ui";
import { api } from "@/lib/api";

import { PanelError, PanelSkeleton } from "./states";

/**
 * The overlay's read size ("read `/dashboard/reports?limit=50` fresh" on every open, wave V8b).
 * `total` is the reports TABLE's count (`backend/app/api/v1/dashboard.py:153`), so up to this
 * many rows the counter is exact — and a larger total must SAY the read was capped.
 */
export const REPORTS_READ_LIMIT = 50;

/**
 * The wire row of the V8/c5 list read — `DailyReportResponse` in `backend/app/schemas/report.py`
 * (id, report_date, morning_plan, evening_summary, tasks_planned, tasks_completed, tasks_failed,
 * updated_at). Declared locally because the shared api module (frozen this wave) does not carry
 * it, the same rule `DailyReportPanel`'s own local declaration records.
 */
export type ReportRow = {
  id: number;
  report_date: string;
  morning_plan: string | null;
  evening_summary: string | null;
  tasks_planned: number;
  tasks_completed: number;
  tasks_failed: number;
  updated_at: string | null;
};

/** `GET /dashboard/reports` answers `{items, total}` — the exact two keys the route returns. */
export type ReportsList = { items: ReportRow[]; total: number };

type DocTab = "all" | "documents" | "reports";

const TAB_ITEMS: TabItem<DocTab>[] = [
  { value: "all", label: "ALL" },
  { value: "documents", label: "DOCUMENTS" },
  { value: "reports", label: "REPORTS" },
];

/**
 * The honest documents-position line — this deployment has no documents store, and the overlay
 * says what does not exist instead of inventing one. It stands on the ALL tab (which must not
 * imply a store either) and is the whole DOCUMENTS tab.
 */
const DOCUMENTS_LINE =
  "No documents store in this deployment — daily reports live under REPORTS.";

/** The task sheet's scrim value, byte-for-byte (sheets-and-composer.md §1) — one token, no new colour. */
const SCRIM = "rgba(0, 0, 0, 0.55)";

/**
 * The Documents overlay: the reports history the Daily Report panel's VIEW ALL opener browses
 * (wave V8b/L3; ui-gap §9 + plan §8 L3).
 *
 * Grammar mirrors `TaskDetailSheet` (never edited): dialog role + `aria-modal`, the one scrim,
 * focus into the panel on open, Esc closes, and focus returns to the opener. Scrim-click-to-close
 * is present here on the brief's pin (the sheet's OPEN DECISION #2 omits it for a data VIEW that
 * holds in-flight writes; this overlay holds only a read, so a stray click costs a re-read).
 * Tab is trapped inside the panel; the Tabs primitive already handles arrow keys for the strip.
 *
 * Every row is a read-only display — there is no per-report page in this route tree, so no row
 * carries a click affordance that would go nowhere.
 */
export function DocumentsOverlay({ open, onClose }: { open: boolean; onClose: () => void }) {
  const titleId = useId();
  const panelRef = useRef<HTMLDivElement>(null);
  const closeButtonRef = useRef<HTMLButtonElement>(null);
  const [tab, setTab] = useState<DocTab>("all");
  const [list, setList] = useState<ReportsList | null>(null);
  const [error, setError] = useState<string | null>(null);

  // Fresh read on every OPEN transition: the overlay keeps no cached answer between opens.
  useEffect(() => {
    if (!open) return;
    setList(null);
    setError(null);
    let cancelled = false;
    api
      .get<ReportsList>(`/dashboard/reports?limit=${REPORTS_READ_LIMIT}`)
      .then((data) => {
        if (!cancelled) setList(data);
      })
      .catch((e: unknown) => {
        if (!cancelled) setError(e instanceof Error ? e.message : String(e));
      });
    return () => {
      cancelled = true;
    };
  }, [open]);

  // Focus moves into the panel on open (the sheet's grammar). Focus-return belongs to the
  // OPENER — DailyReportPanel re-focuses its VIEW ALL button on every close path, so the return
  // target is correct even when the overlay never observed the click (a keyboard-shortcut open).
  useEffect(() => {
    if (open) closeButtonRef.current?.focus();
  }, [open]);

  if (!open) return null;

  const items = list?.items ?? [];
  const total = list?.total ?? 0;
  const showsRows = tab !== "documents";
  // N = the report rows the active tab is showing; TOTAL = the server's total field. Both render
  // only once the read has answered: "not read yet" and "failed" have no real numbers to state.
  const shown = showsRows ? items.length : 0;
  const capped = showsRows && total > REPORTS_READ_LIMIT;
  const counter =
    list !== null
      ? `${shown} OF ${total}${capped ? ` — showing the newest ${items.length}` : ""}`
      : null;

  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.key === "Escape") {
      onClose();
      return;
    }
    if (event.key !== "Tab") return;
    // Tab is trapped inside the dialog (the brief's a11y pin). The strip's roving tabIndex keeps
    // exactly one tab in the tab order, so the trap walks: ✕ ↔ the active segment.
    const panel = panelRef.current;
    if (!panel) return;
    const focusables = Array.from(
      panel.querySelectorAll<HTMLElement>(
        'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])',
      ),
    ).filter((element) => !element.hasAttribute("disabled") && element.tabIndex >= 0);
    if (focusables.length === 0) return;
    const first = focusables[0];
    const last = focusables[focusables.length - 1];
    const active = document.activeElement;
    if (event.shiftKey) {
      if (active === first || !panel.contains(active)) {
        event.preventDefault();
        last.focus();
      }
    } else if (active === last || !panel.contains(active)) {
      event.preventDefault();
      first.focus();
    }
  };

  return (
    <div
      className="fixed inset-0 z-50 flex items-start justify-center overflow-y-auto p-4"
      style={{ backgroundColor: SCRIM }}
      onClick={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <div
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        onKeyDown={onKeyDown}
        className="card-surface my-4 w-full max-w-[637px] shadow-2xl"
      >
        <header className="flex items-center gap-2 border-b border-line px-4 py-2">
          <h2 id={titleId} className="text-base font-bold leading-tight text-ink">
            Documents
          </h2>
          <Button
            ref={closeButtonRef}
            size="sm"
            aria-label="Close documents"
            className="ml-auto"
            onClick={onClose}
          >
            ✕
          </Button>
        </header>

        <div className="max-h-[70vh] overflow-y-auto px-4 py-3">
          <Tabs items={TAB_ITEMS} value={tab} onChange={setTab} ariaLabel="Document sections" />

          {counter !== null ? <p className="ui-label mt-3 text-ink-muted">{counter}</p> : null}

          {list === null && error === null ? (
            <div className="mt-3">
              <PanelSkeleton rows={4} />
            </div>
          ) : null}
          {list === null && error !== null ? (
            <div className="mt-3">
              <PanelError label="the reports list" message={error} />
            </div>
          ) : null}
          {list !== null ? <ReportRows items={items} tab={tab} /> : null}
        </div>
      </div>
    </div>
  );
}

/**
 * The tab bodies. REPORTS and ALL show the report rows; DOCUMENTS is the honest empty state; ALL
 * additionally carries the standing documents-position line (rendered after the rows, so the
 * real receipts lead and the note explains what the ALL view does not contain).
 */
function ReportRows({ items, tab }: { items: ReportRow[]; tab: DocTab }) {
  if (tab === "documents") {
    return <p className="mt-3 text-sm text-ink-muted">{DOCUMENTS_LINE}</p>;
  }
  return (
    <div className="mt-3">
      {items.length === 0 ? (
        <p className="text-sm text-ink-muted">
          No daily reports yet. The daily cycle writes one after the evening run — check back once
          it has run.
        </p>
      ) : (
        <ul className="space-y-2">
          {items.map((row) => (
            <li key={row.id} className="border border-line bg-surface-sunk p-3">
              <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
                <time dateTime={row.report_date} className="ui-label text-ink">
                  {row.report_date}
                </time>
                <span className="ui-label text-ink-muted">
                  {row.tasks_planned} planned · {row.tasks_completed} done · {row.tasks_failed}{" "}
                  failed
                </span>
              </div>
              {/* A null morning_plan renders NOTHING — no filler line, no dash, no zero. */}
              {row.morning_plan ? (
                <p className="mt-1 line-clamp-1 text-sm text-ink-muted">{row.morning_plan}</p>
              ) : null}
            </li>
          ))}
        </ul>
      )}
      {tab === "all" ? <p className="mt-2 text-sm text-ink-muted">{DOCUMENTS_LINE}</p> : null}
    </div>
  );
}

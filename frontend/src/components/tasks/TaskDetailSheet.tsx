"use client";

import { useEffect, useId, useRef, useState } from "react";
import type { ReactNode } from "react";

import { absoluteStamp, agentLabel, NOT_MEASURED, relativeTime } from "@/components/dashboard/format";
import { Button, Chip, cn } from "@/components/ui";
import { api } from "@/lib/api";

import { fingerprintsFrom } from "./approval";
import { recoveryFor } from "./recovery";
import { costUsdText, tokenText } from "./runTruth";
import { errorText, statusChipClass, statusLabel } from "./status";
import type { AgentTriggerResponse, ApprovalIssueResponse, TaskRow } from "./types";

/**
 * Measured scrim (sheets-and-composer.md §1): ≈55% black, ONE token for every sheet. The two
 * live measurements read ~50% and ~55%; the cap-12 "not dimmed" exception is deliberately not
 * copied.
 */
const SCRIM = "rgba(0, 0, 0, 0.55)";

type Notice = { tone: "status" | "alert"; text: string };

/**
 * The task detail overlay (SURFACE 4 → our route-adapted sheet), built to the conventions in
 * sheets-and-composer.md §1: white panel (max 637px — the measured live width), 1px border,
 * shadow, centred over a full-viewport scrim; `role="dialog"` + `aria-modal`; focus moves into
 * the sheet and returns to the control that opened it (B17); `✕` and `Esc` close. Esc and
 * focus-return are OUR additions (the live captures only prove the ✕); scrim-click-to-close is
 * deliberately absent (OPEN DECISION #2 — an accidental dismissal of a data view is worse).
 */
export function TaskDetailSheet({
  task,
  onClose,
  onTaskQueued,
}: {
  task: TaskRow;
  onClose: () => void;
  /** Called after a write that changes the board (a queued run / a minted approval). */
  onTaskQueued?: () => void;
}) {
  const titleId = useId();
  const closeButtonRef = useRef<HTMLButtonElement>(null);
  const openerRef = useRef<HTMLElement | null>(null);
  const [busyAction, setBusyAction] = useState<string | null>(null);
  const [notice, setNotice] = useState<Notice | null>(null);

  useEffect(() => {
    openerRef.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    closeButtonRef.current?.focus();
    return () => {
      openerRef.current?.focus();
    };
  }, []);

  const fingerprints = fingerprintsFrom(task.result_summary);
  /**
   * B3 (wave U3): ONE recovery grammar for this card's state — the footer below renders the
   * single action `recoveryFor` names (run / approve) or the stated reason why there is none.
   * Two competing CTAs on one card is the defect this replaced: the old sheet offered `Run now`
   * on EVERY non-parked status, including completed/failed/rejected rows it cannot honestly
   * re-run.
   */
  const recovery = recoveryFor(task);
  const descriptionLines = splitDescription(task.description);

  /**
   * The run action (runs under the label `recoveryFor` chose — `Run now` for a queued row,
   * `Run again` for a failed one, B3). The real verb mapping (tasks.md §2):
   * `POST /agents/{agent_type}/trigger` CREATES a new task row (`source="manual"`) and
   * dispatches it; it never re-runs this row, and the helper line above the button says so. The
   * server's `message` is rendered verbatim (it names the new task id); a refusal renders
   * `ApiError.display`, never a friendlier rewrite.
   */
  const runNow = async () => {
    setBusyAction("run");
    setNotice(null);
    try {
      const result = await api.post<AgentTriggerResponse>(`/agents/${task.agent_type}/trigger`, {
        task_title: task.title,
        description: task.description ?? undefined,
        priority: task.priority ?? undefined,
      });
      setNotice({ tone: "status", text: result.message });
      onTaskQueued?.();
    } catch (e) {
      setNotice({ tone: "alert", text: errorText(e) });
    } finally {
      setBusyAction(null);
    }
  };

  /**
   * The approval path (T02b): minting a token for the parked row's fingerprint and attaching it
   * to THIS task re-queues it server-side, so the board is refreshed after a success. The
   * response carries a token; it is never rendered — only the mint fact and its expiry are.
   */
  const approve = async (fingerprint: string) => {
    setBusyAction(`approve:${fingerprint}`);
    setNotice(null);
    try {
      const result = await api.post<ApprovalIssueResponse>("/approvals", {
        action_fingerprint: fingerprint,
        task_id: task.id,
      });
      const expires = Number.isFinite(result.expires_at)
        ? absoluteStamp(new Date(result.expires_at * 1000).toISOString())
        : "date unknown";
      setNotice({
        tone: "status",
        text: `Approval minted and attached to task #${task.id}; expires ${expires}. The task was re-queued.`,
      });
      onTaskQueued?.();
    } catch (e) {
      setNotice({ tone: "alert", text: errorText(e) });
    } finally {
      setBusyAction(null);
    }
  };

  return (
    <div
      className="fixed inset-0 z-50 flex items-start justify-center overflow-y-auto p-4"
      style={{ backgroundColor: SCRIM }}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        onKeyDown={(e) => {
          if (e.key === "Escape") onClose();
        }}
        className="card-surface my-4 w-full max-w-[637px] shadow-2xl"
      >
        <header className="flex items-center gap-2 border-b border-line px-4 py-2">
          <button type="button" className="ui-label text-link hover:underline" onClick={onClose}>
            {/* Breadcrumb `‹ ALL TASKS` (SURFACE 4) — on a route this is the sheet's back control. */}
            <span aria-hidden>‹</span> All tasks
          </button>
          <Button
            ref={closeButtonRef}
            size="sm"
            aria-label="Close task detail"
            className="ml-auto"
            onClick={onClose}
          >
            ✕
          </Button>
        </header>

        <div className="max-h-[70vh] overflow-y-auto px-4 py-3">
          {/* The live sheet title-cases the heading; per OPEN DECISION #1 the wire value is
              preserved exactly and the case treatment is presentation only. */}
          <h2 id={titleId} className="text-xl font-bold leading-snug text-ink">
            {task.title}
          </h2>

          <dl className="mt-3 space-y-1">
            <MetaRow label="Status">
              <Chip variant="soft" className={statusChipClass(task.status)}>
                {statusLabel(task.status)}
              </Chip>
            </MetaRow>
            <MetaRow label="Agent">
              <span title={task.agent_type}>{agentLabel(task.agent_type)}</span>
            </MetaRow>
            <MetaRow label="Created">
              <span title={absoluteStamp(task.created_at)}>{relativeTime(task.created_at)}</span>
            </MetaRow>
            {/* `completed_at` lives in the Run truth section below (B4) — one place, no
                duplication; a task that never completed shows no Completed row there either. */}
            {/* Ours-additive real wire fields (tasks.md §2); SOURCE omitted while null, PRIORITY
                keeps the em dash for a null (never a fabricated 3). */}
            {task.source ? <MetaRow label="Source">{task.source}</MetaRow> : null}
            <MetaRow label="Priority">{task.priority ?? NOT_MEASURED}</MetaRow>
          </dl>

          {/* ── Run truth (B4, wave U3) ─────────────────────────────────────────────────────
              The measured facts off this task's rows, one layer under any price-shaped label
              with nothing priced here. Every value is rendered exactly as the server recorded
              it: `cost_usd` / `tokens_*` arrive as NULL when unmeasured (a null cost is the dash
              PLUS its reason — never $0.00-for-unknown), `completed_at` exists only for a
              completed task. An old payload without the keys behaves exactly like a null one. */}
          <section className="mt-3" data-testid="run-truth">
            <p className="ui-label text-ink-muted">Run truth</p>
            <dl className="mt-1 space-y-1 border border-line bg-surface-sunk p-3">
              {task.completed_at ? (
                <MetaRow label="Completed">
                  <span title={absoluteStamp(task.completed_at)}>{relativeTime(task.completed_at)}</span>
                </MetaRow>
              ) : task.status === "completed" ? (
                <MetaRow label="Completed">
                  <span title="the task is completed, but its row carries no completion timestamp">
                    — not recorded
                  </span>
                </MetaRow>
              ) : null}
              {costUsdText(task.cost_usd) !== null ? (
                <MetaRow label="Cost (USD)">{costUsdText(task.cost_usd)}</MetaRow>
              ) : (
                <MetaRow label="Cost (USD)">
                  <span title="the API serves null when a task has no recorded run totals — unmeasured is never a zero">
                    — not measured — the server recorded no run totals for this task
                  </span>
                </MetaRow>
              )}
              {tokenText(task.tokens_in) !== null ? (
                <MetaRow label="Tokens in">{tokenText(task.tokens_in)}</MetaRow>
              ) : null}
              {tokenText(task.tokens_out) !== null ? (
                <MetaRow label="Tokens out">{tokenText(task.tokens_out)}</MetaRow>
              ) : null}
            </dl>
            <p className="mt-1 text-xs text-ink-muted">
              Measured from this task&rsquo;s runs ledger exactly as the server recorded it — no
              estimate and no pricing is applied here. A dash means nothing was recorded; a dash
              is never a zero.
            </p>
          </section>

          {descriptionLines.length > 0 ? (
            <section className="mt-3">
              <p className="ui-label text-ink-muted">Description</p>
              <div className="mt-1 border border-line bg-surface-sunk p-3 text-sm text-ink">
                {descriptionLines.length === 1 ? (
                  <p>{descriptionLines[0]}</p>
                ) : (
                  <ul className="list-disc space-y-1 pl-4">
                    {descriptionLines.map((line) => (
                      <li key={line}>{line}</li>
                    ))}
                  </ul>
                )}
              </div>
            </section>
          ) : null}

          {task.result_summary ? (
            <section className="mt-3">
              <p className="ui-label text-ink-muted">Result</p>
              <div className="mt-1 whitespace-pre-wrap border border-line bg-surface-sunk p-3 text-sm text-ink">
                {task.result_summary}
              </div>
            </section>
          ) : null}

          {task.error_message ? (
            <section className="mt-3">
              <p className="ui-label text-alert">Error</p>
              <div className="mt-1 whitespace-pre-wrap border border-alert p-3 text-sm text-ink">
                {task.error_message}
              </div>
            </section>
          ) : null}

          {notice ? (
            <div
              role={notice.tone}
              data-tone={notice.tone}
              className={cn(
                "mt-3 border p-3",
                notice.tone === "alert" ? "border-alert bg-surface" : "border-line-strong bg-surface-sunk",
              )}
            >
              <p className="text-sm text-ink">{notice.text}</p>
            </div>
          ) : null}

          {recovery.kind === "approve" ? (
            <section data-recovery="approve" className="mt-3 border border-line bg-surface-sunk p-3">
              <p className="ui-label text-ink">Approval</p>
              <p className="mt-1 text-sm text-ink-muted">
                {fingerprints.length === 1
                  ? "1 gated action needs a token."
                  : `${fingerprints.length} gated actions need tokens.`}{" "}
                Approving mints a token for the action, attaches it to this task and re-queues it.
              </p>
              <ul className="mt-2 space-y-2">
                {fingerprints.map((fingerprint) => (
                  <li key={fingerprint} className="flex items-center gap-2">
                    <code className="min-w-0 truncate font-mono text-xs text-ink" title={fingerprint}>
                      {fingerprint}
                    </code>
                    <Button
                      variant="glossy"
                      size="sm"
                      className="ml-auto"
                      disabled={busyAction !== null}
                      onClick={() => approve(fingerprint)}
                    >
                      {busyAction === `approve:${fingerprint}` ? "Approving…" : "Approve"}
                    </Button>
                  </li>
                ))}
              </ul>
            </section>
          ) : null}

          {recovery.kind === "run" ? (
            <div data-recovery="run" className="mt-4 flex items-center gap-3 border-t border-line pt-3">
              <p className="ui-label mr-auto text-ink-muted">{recovery.helper}</p>
              <Button variant="glossy" disabled={busyAction !== null} onClick={runNow}>
                {busyAction === "run" ? "Queueing…" : recovery.label}
              </Button>
            </div>
          ) : null}

          {recovery.kind === "none" ? (
            // The stated reason, in the same footer slot — for a state with no endpoint behind an
            // action, saying why beats a control that could not work (B3; also covers the parked
            // row whose summary carries no readable fingerprint — tasks.md OPEN DECISION #5).
            <div data-recovery="none" className="mt-4 border-t border-line pt-3">
              <p className="ui-label text-ink-muted">Action</p>
              <p className="mt-1 text-sm text-ink-muted">{recovery.reason}</p>
            </div>
          ) : null}

          {/* Refused by name, not faked (tasks.md §2): the three controls the live sheet shows
              that have no endpoint here. Worded so the page carries no dead-control tokens. */}
          <div className="mt-4 border-t border-line pt-3">
            <p className="ui-label text-ink-muted">Not in this build</p>
            <ul className="mt-1 list-disc space-y-1 pl-4 text-xs text-ink-muted">
              <li>Repeat (re-running this exact row): no endpoint exists — a new run means a new task.</li>
              <li>
                Cancel: the status-change route exists on the server, but this build&rsquo;s API
                client has no PATCH method, so no control is drawn for it.
              </li>
              <li>
                Changing a task&rsquo;s text, or removing a task: no endpoint exists anywhere in the
                API — no controls are drawn for them.
              </li>
            </ul>
          </div>
        </div>
      </div>
    </div>
  );
}

function MetaRow({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="flex items-baseline gap-2">
      <dt className="ui-label w-20 shrink-0 text-ink-muted">{label}</dt>
      <dd className="text-sm text-ink">{children}</dd>
    </div>
  );
}

/**
 * DESCRIPTION panel lines. The wire carries ONE string while the live sheet shows a bullet
 * list (SURFACE 4), so it is split on line breaks with leading bullet markers stripped; a
 * single-line description renders as one paragraph (tasks.md §2).
 */
function splitDescription(description: string | null | undefined): string[] {
  if (!description) return [];
  return description
    .split(/\r?\n/)
    .map((line) => line.replace(/^\s*[-•*]\s+/, "").trim())
    .filter((line) => line.length > 0);
}

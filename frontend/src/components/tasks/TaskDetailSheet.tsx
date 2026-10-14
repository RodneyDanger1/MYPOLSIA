"use client";

import { useEffect, useId, useRef, useState } from "react";
import type { ReactNode } from "react";

import { absoluteStamp, agentLabel, NOT_MEASURED, relativeTime } from "@/components/dashboard/format";
import { Button, Chip, cn } from "@/components/ui";
import { api } from "@/lib/api";

import { fingerprintsFrom } from "./approval";
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
  const parked = task.status === "awaiting_approval";
  const descriptionLines = splitDescription(task.description);

  /**
   * `RUN NOW` — the real verb mapping (tasks.md §2): `POST /agents/{agent_type}/trigger` CREATES
   * a new task row (`source="manual"`) and dispatches it; it never re-runs this row, and the
   * helper line in the action row says so. The server's `message` is rendered verbatim (it names
   * the new task id); a refusal renders `ApiError.display`, never a friendlier rewrite.
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
            {/* `completed_at` is added to TaskResponse by B1 this wave; until the wire carries it
                the row is omitted (never a fabricated "completed" claim — cap-06's COMPLETED row
                has no field today). */}
            {task.completed_at ? (
              <MetaRow label="Completed">
                <span title={absoluteStamp(task.completed_at)}>{relativeTime(task.completed_at)}</span>
              </MetaRow>
            ) : null}
            {/* Ours-additive real wire fields (tasks.md §2); SOURCE omitted while null, PRIORITY
                keeps the em dash for a null (never a fabricated 3). */}
            {task.source ? <MetaRow label="Source">{task.source}</MetaRow> : null}
            <MetaRow label="Priority">{task.priority ?? NOT_MEASURED}</MetaRow>
          </dl>

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

          {parked && fingerprints.length > 0 ? (
            <section className="mt-3 border border-line bg-surface-sunk p-3">
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

          {parked && fingerprints.length === 0 ? (
            // An `awaiting_approval` row whose summary carries no readable fingerprint: say so,
            // never render a control that would post garbage (tasks.md OPEN DECISION #5).
            <p className="mt-3 border border-line-strong bg-surface-sunk p-3 text-sm text-ink-muted">
              This task is parked awaiting approval, but its result text carries no fingerprint
              this build can read — there is nothing to approve from here.
            </p>
          ) : null}

          {!parked ? (
            <div className="mt-4 flex items-center gap-3 border-t border-line pt-3">
              <p className="ui-label mr-auto text-ink-muted">
                Run now queues a NEW task for this agent (it spends from today&rsquo;s AI budget);
                this row is never re-run.
              </p>
              <Button variant="glossy" disabled={busyAction !== null} onClick={runNow}>
                {busyAction === "run" ? "Queueing…" : "Run now"}
              </Button>
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

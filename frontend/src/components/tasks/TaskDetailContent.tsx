"use client";

import { useEffect, useId, useState } from "react";
import type { ReactNode } from "react";

import { absoluteStamp, agentLabel, NOT_MEASURED, relativeTime } from "@/components/dashboard/format";
import { readErrorText } from "@/components/dashboard/states";
import { Button, Chip, cn } from "@/components/ui";
import { api } from "@/lib/api";

import { runThisTask } from "./actions";
import { fingerprintsFrom } from "./approval";
import { recoveryFor } from "./recovery";
import { costUsdText, tokenText } from "./runTruth";
import { errorText, statusChipClass, statusLabel } from "./status";
import { TaskStatusControl } from "./TaskStatusControl";
import type { AgentTriggerResponse, ApprovalIssueResponse, TaskRow } from "./types";

/**
 * The task detail BODY — every field and action of one task, in ONE place. Extracted from
 * `TaskDetailSheet` in wave V11 unit 2 (the sheet keeps scrim/dialog/focus and renders this;
 * the new `/tasks/[id]` route renders it as a page) so the two detail surfaces cannot fork:
 * a field added here appears on both, by construction.
 *
 * The extraction moved the sheet's inner DOM byte-for-byte (the u3/u2/u4 pins on the sheet —
 * `.btn-glossy` counts, `data-testid="run-truth"`, `data-recovery`, the heading — all still
 * query the same nodes one component deeper).
 *
 * Local override: after a successful status write the SERVER's returned row is rendered
 * immediately (`updated ?? task`); when the owning surface updates `task` (its refetch), the
 * override clears — the prop is always the fresher read.
 */
export function TaskDetailContent({
  task,
  onTaskQueued,
  titleId,
}: {
  task: TaskRow;
  /** Called after a write that changes the board (a queued run / a minted approval / a status set). */
  onTaskQueued?: () => void;
  /** The sheet's dialog labels itself with this id when it renders the heading. */
  titleId?: string;
}) {
  const ownTitleId = useId();
  const headingId = titleId ?? ownTitleId;
  const [busyAction, setBusyAction] = useState<string | null>(null);
  const [notice, setNotice] = useState<{ tone: "status" | "alert"; text: string } | null>(null);
  const [updated, setUpdated] = useState<TaskRow | null>(null);
  /** V24/L1 — the "Run this task" dispatch (the EXISTING row, not a new one). */
  const [runThisBusy, setRunThisBusy] = useState(false);
  const [runThisLatched, setRunThisLatched] = useState(false);

  useEffect(() => {
    setUpdated(null);
  }, [task]);

  const row = updated ?? task;

  useEffect(() => {
    // A real state change (the server moved the row) re-arms the dispatch control; until then
    // the latch prevents a second enqueue of the same pending row.
    if (row.status !== "pending") setRunThisLatched(false);
  }, [row.status]);
  const fingerprints = fingerprintsFrom(row.result_summary);
  /**
   * B3 (wave U3): ONE recovery grammar for this card's state — the footer below renders the
   * single action `recoveryFor` names (run / approve) or the stated reason why there is none.
   * Two competing CTAs on one card is the defect this replaced. The status-write control
   * (V11) is a SEPARATE section — it is not a recovery action (nothing "recovers" a task by
   * renaming its state) and it stays out of this grammar on purpose.
   */
  const recovery = recoveryFor(row);
  const descriptionLines = splitDescription(row.description);

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
      const result = await api.post<AgentTriggerResponse>(`/agents/${row.agent_type}/trigger`, {
        task_title: row.title,
        description: row.description ?? undefined,
        priority: row.priority ?? undefined,
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
   * V24/L1 — `Run this task`: dispatch the EXISTING row (`POST /tasks/{id}/run`, 202). The
   * success notice quotes the server (`{task_id, status:"dispatched"}`) and then calls
   * `onTaskQueued`, so the sheet/page re-reads the row; the status shown is always the
   * server's, never an optimistic `in_progress`. A refusal renders `readErrorText`.
   */
  const runThis = async () => {
    if (runThisBusy || runThisLatched) return;
    setRunThisBusy(true);
    setNotice(null);
    try {
      const result = await runThisTask(row);
      setRunThisLatched(true);
      setNotice({
        tone: "status",
        text: `Task #${result.task_id} ${result.status} — the worker will claim it; the status below is still the server's.`,
      });
      onTaskQueued?.();
    } catch (e) {
      setNotice({ tone: "alert", text: readErrorText(e) });
    } finally {
      setRunThisBusy(false);
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
        task_id: row.id,
      });
      const expires = Number.isFinite(result.expires_at)
        ? absoluteStamp(new Date(result.expires_at * 1000).toISOString())
        : "date unknown";
      setNotice({
        tone: "status",
        text: `Approval minted and attached to task #${row.id}; expires ${expires}. The task was re-queued.`,
      });
      onTaskQueued?.();
    } catch (e) {
      setNotice({ tone: "alert", text: errorText(e) });
    } finally {
      setBusyAction(null);
    }
  };

  return (
    <>
      {/* The live sheet title-cases the heading; per OPEN DECISION #1 the wire value is
          preserved exactly and the case treatment is presentation only. */}
      <h2 id={headingId} className="text-xl font-bold leading-snug text-ink">
        {row.title}
      </h2>

      <dl className="mt-3 space-y-1">
        <MetaRow label="Status">
          <Chip variant="soft" className={statusChipClass(row.status)}>
            {statusLabel(row.status)}
          </Chip>
        </MetaRow>
        <MetaRow label="Agent">
          <span title={row.agent_type}>{agentLabel(row.agent_type)}</span>
        </MetaRow>
        <MetaRow label="Created">
          <span title={absoluteStamp(row.created_at)}>{relativeTime(row.created_at)}</span>
        </MetaRow>
        {/* `completed_at` lives in the Run truth section below (B4) — one place, no
            duplication; a task that never completed shows no Completed row there either. */}
        {/* Ours-additive real wire fields (tasks.md §2); SOURCE omitted while null, PRIORITY
            keeps the em dash for a null (never a fabricated 3). */}
        {row.source ? <MetaRow label="Source">{row.source}</MetaRow> : null}
        <MetaRow label="Priority">{row.priority ?? NOT_MEASURED}</MetaRow>
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
          {row.completed_at ? (
            <MetaRow label="Completed">
              <span title={absoluteStamp(row.completed_at)}>{relativeTime(row.completed_at)}</span>
            </MetaRow>
          ) : row.status === "completed" ? (
            <MetaRow label="Completed">
              <span title="the task is completed, but its row carries no completion timestamp">
                — not recorded
              </span>
            </MetaRow>
          ) : null}
          {costUsdText(row.cost_usd) !== null ? (
            <MetaRow label="Cost (USD)">{costUsdText(row.cost_usd)}</MetaRow>
          ) : (
            <MetaRow label="Cost (USD)">
              <span title="the API serves null when a task has no recorded run totals — unmeasured is never a zero">
                — not measured — the server recorded no run totals for this task
              </span>
            </MetaRow>
          )}
          {tokenText(row.tokens_in) !== null ? (
            <MetaRow label="Tokens in">{tokenText(row.tokens_in)}</MetaRow>
          ) : null}
          {tokenText(row.tokens_out) !== null ? (
            <MetaRow label="Tokens out">{tokenText(row.tokens_out)}</MetaRow>
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

      {row.result_summary ? (
        <section className="mt-3">
          <p className="ui-label text-ink-muted">Result</p>
          <div className="mt-1 whitespace-pre-wrap border border-line bg-surface-sunk p-3 text-sm text-ink">
            {row.result_summary}
          </div>
        </section>
      ) : null}

      {row.error_message ? (
        <section className="mt-3">
          <p className="ui-label text-alert">Error</p>
          <div className="mt-1 whitespace-pre-wrap border border-alert p-3 text-sm text-ink">
            {row.error_message}
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

      {/* ── V24/L1: run THIS row ────────────────────────────────────────────────────────
          Separate from the B3 recovery footer on purpose: the footer's `Run now` creates a
          NEW task (its helper says so), while this control dispatches the queued row itself.
          Pending only — every other state has nothing dispatchable. Default variant (not
          `.btn-glossy`): one PRIMARY per card, as the shipped pins require. */}
      {row.status === "pending" ? (
        <section data-testid="run-this-task" className="mt-3 border border-line bg-surface-sunk p-3">
          <p className="ui-label text-ink">Execute this task</p>
          <p className="mt-1 text-sm text-ink-muted">
            Run this task dispatches <strong>this</strong> queued row to the worker queue — it
            does not create a new task. The server moves it to in_progress when a worker claims
            it; the detail re-reads after the dispatch and never assumes that transition.
          </p>
          <div className="mt-2 flex items-center gap-2">
            <Button
              size="sm"
              aria-busy={runThisBusy || undefined}
              disabled={runThisBusy || runThisLatched}
              onClick={runThis}
            >
              {runThisBusy ? "Dispatching…" : "Run this task"}
            </Button>
          </div>
        </section>
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

      {/* ── The status write (V11 unit 2) ───────────────────────────────────────────────
          `PATCH /tasks/{id}/status` via the frozen `api.patch` — the control offers exactly
          the seven values the service's guard accepts and renders refusals by name. It calls
          `onTaskQueued` too: a visible state change is a board change. */}
      <TaskStatusControl
        task={row}
        onChanged={(serverRow) => {
          setUpdated(serverRow);
          onTaskQueued?.();
        }}
      />

      {/* Refused by name, not faked (tasks.md §2): the controls the live sheet shows
          that have no endpoint here. Worded so the page carries no dead-control tokens. The
          status-change route that used to be listed here now HAS a control (above). */}
      <div className="mt-4 border-t border-line pt-3">
        <p className="ui-label text-ink-muted">Not in this build</p>
        <ul className="mt-1 list-disc space-y-1 pl-4 text-xs text-ink-muted">
          <li>
            Changing a task&rsquo;s text, or removing a task: no endpoint exists anywhere in the
            API — no controls are drawn for them.
          </li>
        </ul>
      </div>
    </>
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

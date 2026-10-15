"use client";

import { useCallback, useState } from "react";

import { NOT_MEASURED, absoluteStamp } from "@/components/dashboard/format";
import { countText } from "@/components/finance/format";
import { ListSkeleton, ReadError } from "@/components/finance/states";
import { useOpsBreaker, useOpsQuality, type OpsQualityRow } from "@/components/usage/useOpsRead";
import { Button, Card, DataTable, type Column } from "@/components/ui";

/**
 * Ops panel (V11 unit 2) — the Usage page's operator surfaces for the activity-breaker's
 * read/reset routes and the per-agent quality read (backend `app/api/v1/ops.py`; wiring audit F5,
 * capability audit B1/B2: these routes shipped with zero production callers).
 *
 * PROCESS BOUNDARY, in the copy itself: the breaker's counters are module globals in
 * `app.core.events`, i.e. PER PROCESS. This panel reads and resets the API process's counters;
 * the Celery worker keeps its own and the 15-minute beat tick writes them to the worker log.
 * Neither surface sees the other's numbers, and the card says so instead of hiding it.
 *
 * Null law per cell: a null `success_rate` / `last_failure_at` / `cost_per_completed_task` renders
 * the app's markers (em dash, "none recorded"), never a fabricated 0; a measured 0 renders as the
 * reading it is. Loading is a busy skeleton, a failure is NAMED (ReadError, ApiError.display) per
 * read, and the reset is a mutation behind an explicit confirm: no dead button, no silent success.
 *
 * Chrome discipline: the em dash is the reserved marker for an unmeasured VALUE and never appears
 * in this panel's static copy (the loading-state dash guard in the usage suite scans body text).
 */

/** success_rate is `completed / (completed + failed)` in [0, 1]; null is "no terminal run in the
 *  window" and renders the app's dash. A measured 0 (every terminal run failed) is a reading. */
function rateText(value: number | null): string {
  if (typeof value !== "number" || !Number.isFinite(value)) return NOT_MEASURED;
  return `${(value * 100).toFixed(1)}%`;
}

/** The per-completed-task average keeps the ledger's NUMERIC(14,4) precision (4 decimals) for the
 *  same reason the usage page's COST column does: at 2dp a measured sub-cent average would print
 *  `$0.00`, a real reading collapsed to zero. Null (no completion measured) is the dash. */
function costPerTaskText(value: number | null): string {
  if (typeof value !== "number" || !Number.isFinite(value)) return NOT_MEASURED;
  return `$${value.toLocaleString("en-US", {
    minimumFractionDigits: 4,
    maximumFractionDigits: 4,
  })}`;
}

/** `last_failure_at` is the newest failed run ALL TIME (ops.py's own window note); null means the
 *  all-time query found no failed run, which is a measured absence and is said plainly. */
function lastFailureText(value: string | null): string {
  return value === null ? "none recorded" : absoluteStamp(value);
}

const QUALITY_COLUMNS: Column<OpsQualityRow>[] = [
  { key: "agent", header: "AGENT", render: (row) => row.agent_type },
  { key: "runs", header: "RUNS (7D)", render: (row) => countText(row.runs_7d) },
  { key: "success", header: "SUCCESS RATE", render: (row) => rateText(row.success_rate) },
  { key: "failure", header: "LAST FAILURE", render: (row) => lastFailureText(row.last_failure_at) },
  {
    key: "cost",
    header: "COST / COMPLETED",
    render: (row) => costPerTaskText(row.cost_per_completed_task),
  },
];

/** One labelled counter, in the house mono-caps label + serif value voice. */
function Stat({ label, value, testId }: { label: string; value: string; testId?: string }) {
  return (
    <div>
      <dt className="ui-label text-ink-muted">{label}</dt>
      <dd className="text-sm text-ink" data-testid={testId}>
        {value}
      </dd>
    </div>
  );
}

export function OpsPanel() {
  const [reloadKey, setReloadKey] = useState(0);
  const [confirming, setConfirming] = useState(false);
  const [resetDone, setResetDone] = useState(false);
  const quality = useOpsQuality(reloadKey);
  const breaker = useOpsBreaker(reloadKey);

  const refresh = useCallback(() => {
    setResetDone(false);
    setReloadKey((n) => n + 1);
  }, []);

  const onStartReset = useCallback(() => {
    setResetDone(false);
    breaker.dismissResetError();
    setConfirming(true);
  }, [breaker]);

  const onCancelReset = useCallback(() => {
    breaker.dismissResetError();
    setConfirming(false);
  }, [breaker]);

  const onConfirmReset = useCallback(async () => {
    const ok = await breaker.reset();
    if (ok) {
      setConfirming(false);
      setResetDone(true);
    }
  }, [breaker]);

  return (
    <div className="space-y-4" data-testid="ops-panel">
      <div className="flex items-end gap-2">
        <p className="mr-auto ui-label text-ink-muted">Operator surfaces</p>
        <button type="button" className="link ui-label" data-testid="ops-refresh" onClick={refresh}>
          Refresh
        </button>
      </div>

      <Card title="Activity breaker" kicker="OPS · API PROCESS" data-testid="ops-breaker">
        <p className="text-sm text-ink-muted" data-testid="ops-breaker-scope">
          These counters are module globals in the API process and are reported and reset per
          process. The Celery worker keeps its own counters, written to the worker log by the
          15-minute beat tick. Neither surface sees the other&apos;s numbers.
        </p>

        {breaker.loading ? <ListSkeleton testId="ops-breaker-loading" /> : null}

        {!breaker.loading && breaker.error !== null ? (
          <ReadError label="the activity breaker" message={breaker.error} />
        ) : null}

        {!breaker.loading && breaker.error === null && breaker.data !== null ? (
          <div className="mt-2 space-y-3">
            <dl className="grid grid-cols-2 gap-x-4 gap-y-2 md:grid-cols-4">
              <Stat label="FAILED PUBLISHES" value={countText(breaker.data.failures)} />
              <Stat label="SKIPPED WHILE OPEN" value={countText(breaker.data.skipped_while_open)} />
              <Stat
                label="BREAKER"
                value={breaker.data.breaker_open ? "OPEN" : "closed"}
                testId="ops-breaker-state"
              />
              <Stat label="COOLDOWN" value={`${breaker.data.cooldown_seconds}s`} />
            </dl>

            <p className="text-sm text-ink" data-testid="ops-breaker-last-error">
              Last error: {breaker.data.last_error ?? "none recorded"}
            </p>

            <p className="text-sm text-ink-muted">
              How long a failed publish mutes further attempts is the cooldown above. While the
              breaker is open, event publishes return immediately having recorded a skip, so an
              outage costs one bounded attempt per cooldown window instead of one stalled attempt
              per event.
            </p>

            {confirming ? (
              <div
                className="border border-line-strong bg-surface-strong px-3 py-2"
                data-testid="ops-reset-guard"
              >
                <p className="text-sm text-ink">
                  Clear the API process&apos;s in-memory breaker and its counters? No ledger row
                  changes, and the worker&apos;s counters are untouched: only the counters this
                  panel reads are zeroed.
                </p>
                {breaker.resetError !== null ? (
                  <p role="alert" className="mt-2 text-sm text-alert" data-testid="ops-reset-error">
                    Reset failed: {breaker.resetError}
                  </p>
                ) : null}
                <div className="mt-2 flex items-center gap-2">
                  <Button
                    size="sm"
                    data-testid="ops-reset-confirm"
                    disabled={breaker.resetting}
                    onClick={onConfirmReset}
                  >
                    {breaker.resetting ? "Resetting…" : "Confirm reset"}
                  </Button>
                  <Button
                    size="sm"
                    data-testid="ops-reset-cancel"
                    disabled={breaker.resetting}
                    onClick={onCancelReset}
                  >
                    Cancel
                  </Button>
                </div>
              </div>
            ) : (
              <div className="flex items-center gap-2">
                <Button size="sm" data-testid="ops-reset" onClick={onStartReset}>
                  Reset breaker
                </Button>
                {resetDone ? (
                  <span className="ui-label text-ink-muted" data-testid="ops-reset-done">
                    Reset done. The counters above are the API process&apos;s post-reset read.
                  </span>
                ) : null}
              </div>
            )}
          </div>
        ) : null}
      </Card>

      <Card title="Agent quality" kicker="OPS · LAST 7 DAYS" data-testid="ops-quality">
        <p className="text-sm text-ink-muted" data-testid="ops-quality-scope">
          Deterministic read of the agent-run ledger, no model in the loop: runs per agent over the
          last 7 days, the success rate of terminal runs (completed against completed plus failed),
          the newest failure all time, and the average cost of a completed task. Null means not
          measured, never zero.
        </p>

        {quality.loading ? <ListSkeleton testId="ops-quality-loading" /> : null}

        {!quality.loading && quality.error !== null ? (
          <ReadError label="the agent quality view" message={quality.error} />
        ) : null}

        {!quality.loading && quality.error === null && quality.data !== null ? (
          <DataTable<OpsQualityRow>
            columns={QUALITY_COLUMNS}
            rows={quality.data.agents}
            rowKey={(row) => row.agent_type}
            empty={
              <p className="text-sm text-ink-muted">
                No agent rows came back in the quality read. The backend reports one row per
                registered agent, including agents with no runs in the window.
              </p>
            }
          />
        ) : null}
      </Card>
    </div>
  );
}

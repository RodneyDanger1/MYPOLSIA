"use client";

import { useCallback, useState } from "react";

import { NOT_MEASURED, absoluteStamp } from "@/components/dashboard/format";
import { countText, textOrDash } from "@/components/finance/format";
import { ListSkeleton, ReadError } from "@/components/finance/states";
import { ExportButton } from "@/components/usage/ExportButton";
import { OpsPanel } from "@/components/usage/OpsPanel";
import { useRuns, type RunsRow } from "@/components/usage/useRuns";
import { Button, Card, DataTable, type Column } from "@/components/ui";

/**
 * Usage — the agent-runs ledger (wave V7, seat S5). The read is the live `GET /api/v1/runs`
 * (U5/B8): `{items, total}` with `total` the FULL filtered count; the export is
 * `GET /api/v1/runs/export?format=csv`, whose `X-Polsia-Row-Count` is the same total by contract.
 *
 * The declared-total law (B8) is the page's spine:
 *   - the footer prints the server's OWN `total` beside the rows this read actually showed — a
 *     truncated read says "Showing the first N of M" with those real numbers, never a claim the
 *     read did not measure;
 *   - NEXT/PREV move by limit/offset and disable exactly at the edges (`offset + shown >= total`
 *     is the last page; `offset === 0` is the first);
 *   - the null-vs-zero law holds per cell: every nullable wire field renders the app's em dash
 *     (via the shared finance/dashboard formatters), never a fabricated 0 — and the cost column
 *     keeps the ledger's NUMERIC(14,4) precision so a measured sub-cent cost cannot collapse to
 *     `$0.00`.
 *
 * States follow the house idioms (finance page): loading is a busy skeleton, a failure is NAMED
 * on the panel (`ReadError`, ApiError.display) and never replaces the rest of the page with a
 * blank — and there is no polling (C7); the explicit Refresh is the only re-read.
 *
 * Below the ledger, the Ops panel (V11 unit 2) surfaces the activity breaker's per-process
 * counters with a guarded reset and the deterministic per-agent quality read —
 * `components/usage/OpsPanel.tsx` carries the process-boundary truth in its copy.
 */

/** The server's own list-page default (`runs.py` DEFAULT_LIMIT=50), passed explicitly. */
const PAGE_SIZE = 50;

/**
 * `cost_usd` is `float | None` off the ledger: null is UNMEASURED and renders the app's dash —
 * never 0. A measured cost keeps the column's NUMERIC(14,4) precision (4 decimals) rather than
 * the house 2-decimal money shape: at 2dp a measured sub-cent cost would print `$0.00`, i.e. a
 * real reading collapsed to zero — the exact collapse the null-vs-zero law forbids. Grouping
 * (thousands separators) is kept from the house money shape.
 */
function costText(value: number | null): string {
  if (typeof value !== "number" || !Number.isFinite(value)) return NOT_MEASURED;
  return `$${value.toLocaleString("en-US", {
    minimumFractionDigits: 4,
    maximumFractionDigits: 4,
  })}`;
}

/** One attempt reading: a null attempt is unmeasured, a present one prints as-is. */
function attemptText(value: number | null): string {
  return value === null ? NOT_MEASURED : String(value);
}

/** The nine ledger columns, in the brief's fixed order; headers in the house mono-caps voice. */
const LEDGER_COLUMNS: Column<RunsRow>[] = [
  { key: "title", header: "TITLE", render: (row) => textOrDash(row.task_title) },
  { key: "agent", header: "AGENT", render: (row) => row.agent_type },
  { key: "status", header: "STATUS", render: (row) => textOrDash(row.status) },
  { key: "started", header: "STARTED", render: (row) => absoluteStamp(row.started_at) },
  { key: "finished", header: "FINISHED", render: (row) => absoluteStamp(row.ended_at) },
  { key: "tokens", header: "TOKENS", render: (row) => countText(row.tokens_used) },
  { key: "cost", header: "COST", render: (row) => costText(row.cost_usd) },
  { key: "source", header: "SOURCE", render: (row) => textOrDash(row.cost_source) },
  { key: "attempt", header: "ATTEMPT", render: (row) => attemptText(row.attempt) },
];

/**
 * The declared-total line (B8's law): every number here is the server's own `total` or the rows
 * this read actually showed. Page 1 speaks the brief's "first N of M"; a full first page of a
 * longer ledger is thereby labelled as capped; later pages name their real slice; a page that
 * outlived its rows (the ledger shrank between reads) says so rather than lying; and a complete
 * ledger says "all N" — nothing was cut off.
 */
function declaredTotalLine(shown: number, total: number, offset: number): string {
  if (total === 0) return "0 runs in the ledger";
  if (shown === 0) return `No rows on this page — ${total} in the ledger`;
  if (offset === 0 && shown === total) return `Showing all ${total}`;
  if (offset === 0) return `Showing the first ${shown} of ${total}`;
  return `Showing rows ${offset + 1}–${offset + shown} of ${total}`;
}

export default function UsagePage() {
  const [offset, setOffset] = useState(0);
  const [reloadKey, setReloadKey] = useState(0);
  const { data, error, loading } = useRuns(`/runs?limit=${PAGE_SIZE}&offset=${offset}`, reloadKey);
  const refresh = useCallback(() => {
    setOffset(0);
    setReloadKey((n) => n + 1);
  }, []);
  // V17/L4: NEXT also requires a row on THIS page. `offset + shown < total` alone stayed true when
  // the ledger shrank between reads (or an offset past the rows came back empty): every further
  // press re-read the same empty page — a control promising rows the page just said it did not get.
  const canNext =
    data !== null && data.items.length > 0 && offset + data.items.length < data.total;

  return (
    <div className="space-y-4">
      <div className="flex items-end gap-2 border-b border-line pb-1">
        <div className="mr-auto">
          <h1 className="text-3xl font-bold text-ink">Usage</h1>
          {/* A static, true description of the view — this endpoint serves no freshness field, so
              no freshness claim is made (an invented timestamp would be a lie). The separator is
              `·`, NOT an em dash: the dash is the reserved marker for an unmeasured VALUE and
              must never appear in page chrome (the loading-state guard in the suite enforces
              exactly that). */}
          <p className="ui-label text-ink-muted">Newest first · {PAGE_SIZE} per page</p>
        </div>
        <button
          type="button"
          className="link ui-label"
          data-testid="usage-refresh"
          onClick={refresh}
        >
          Refresh
        </button>
      </div>

      <Card title="Runs" data-testid="usage-ledger" action={<ExportButton />}>
        {loading ? <ListSkeleton testId="usage-loading" /> : null}

        {!loading && error !== null ? (
          <ReadError label="the usage ledger" message={error} />
        ) : null}

        {!loading && error === null && data !== null ? (
          <DataTable<RunsRow>
            columns={LEDGER_COLUMNS}
            rows={data.items}
            rowKey={(row) => String(row.id)}
            empty={
              <p className="text-sm text-ink-muted">
                No runs recorded yet — every agent run writes a ledger row here; dispatch an agent
                from the Agents page.
              </p>
            }
            footer={
              <div className="flex items-center gap-2">
                <span className="ui-label text-ink-muted" data-testid="usage-count">
                  {declaredTotalLine(data.items.length, data.total, offset)}
                </span>
                <span className="ml-auto flex items-center gap-2">
                  <Button
                    size="sm"
                    data-testid="usage-prev"
                    disabled={offset === 0}
                    onClick={() => setOffset((current) => Math.max(0, current - PAGE_SIZE))}
                  >
                    Prev
                  </Button>
                  <Button
                    size="sm"
                    data-testid="usage-next"
                    disabled={!canNext}
                    onClick={() => setOffset((current) => current + PAGE_SIZE)}
                  >
                    Next
                  </Button>
                </span>
              </div>
            }
          />
        ) : null}
      </Card>

      {/* V11-W6 (unit 2): the operator surfaces for the ledger's writers. The breaker card reads
          and resets the API process's counters (per-process truth spelled out in its copy); the
          quality card is the deterministic per-agent read. */}
      <OpsPanel />
    </div>
  );
}

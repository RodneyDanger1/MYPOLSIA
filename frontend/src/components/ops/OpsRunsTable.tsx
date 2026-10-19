"use client";

import Link from "next/link";

import { ListSkeleton, ReadError } from "@/components/finance/states";
import { NOT_MEASURED } from "@/components/dashboard/format";
import { Card, Chip, DataTable, type Column } from "@/components/ui";

import { type OpsRunRow, type OpsToolsCell, useOpsRuns } from "./useOpsRuns";

/**
 * The /ops runs table — the live ledger with the one detail-only fact the cockpit needs.
 *
 * Every column is served by the API: `id` links to the shipped run detail, `task_title` is the
 * joined task, `agent_type`/`status` are the stored words, `cost_usd` keeps the ledger's
 * NUMERIC(14,4) precision (a measured sub-cent cost must not collapse to `$0.00`), and a null cost
 * is the em dash — never a fabricated zero. `duration_secs` is printed as a real elapsed span, and
 * its null is the dash. `tools_used` comes from the run's own detail read and keeps all three
 * states distinct (see `useOpsRuns`).
 */
function costText(value: number | null): string {
  if (typeof value !== "number" || !Number.isFinite(value)) return NOT_MEASURED;
  return `$${value.toLocaleString("en-US", {
    minimumFractionDigits: 4,
    maximumFractionDigits: 4,
  })}`;
}

/** `duration_secs` as seconds/minutes/hours; null (still running, or unrecorded) is the dash. */
export function durationText(value: number | null): string {
  if (typeof value !== "number" || !Number.isFinite(value) || value < 0) return NOT_MEASURED;
  const total = Math.round(value);
  if (total < 60) return `${total}s`;
  const minutes = Math.floor(total / 60);
  const seconds = total % 60;
  if (minutes < 60) return `${minutes}m ${seconds}s`;
  const hours = Math.floor(minutes / 60);
  return `${hours}h ${minutes % 60}m`;
}

function ToolsCell({ runId, cell }: { runId: number; cell: OpsToolsCell | undefined }) {
  if (cell === undefined) {
    return (
      <span className="ui-label text-ink-muted" data-testid={`ops-tools-${runId}`}>
        reading…
      </span>
    );
  }
  if (cell.kind === "error") {
    return (
      <span
        className="ui-label text-alert"
        data-testid={`ops-tools-${runId}`}
        title={`the run's detail read failed: ${cell.message}`}
      >
        — tools unread
      </span>
    );
  }
  if (cell.tools === null) {
    return (
      <span
        className="ui-label text-ink-muted"
        data-testid={`ops-tools-${runId}`}
        title="the run record stores no tool list (never recorded) — this is not the same as 'no tool ran'"
      >
        — never recorded
      </span>
    );
  }
  if (cell.tools.length === 0) {
    return (
      <span
        className="ui-label text-ink-muted"
        data-testid={`ops-tools-${runId}`}
        title="the run recorded an empty tool list: tools were available and the model called none"
      >
        none
      </span>
    );
  }
  const shown = cell.tools.slice(0, 4);
  return (
    <span
      className="flex flex-wrap items-center gap-1"
      data-testid={`ops-tools-${runId}`}
      title={cell.tools.join(", ")}
    >
      {shown.map((tool) => (
        <Chip key={tool} variant="soft" className="font-mono text-[10px]">
          {tool}
        </Chip>
      ))}
      {cell.tools.length > shown.length ? (
        <span className="ui-label text-ink-muted">+{cell.tools.length - shown.length}</span>
      ) : null}
    </span>
  );
}

const COLUMNS: Column<OpsRunRow & { toolsCell?: OpsToolsCell }>[] = [
  {
    key: "id",
    header: "RUN",
    render: (row) => (
      <Link
        href={`/runs/${row.id}`}
        className="ui-label text-link hover:underline"
        aria-label={`Open run ${row.id}`}
      >
        #{row.id}
      </Link>
    ),
  },
  {
    key: "task",
    header: "TASK",
    render: (row) => (
      <span title={row.task_title ?? undefined}>{row.task_title ?? NOT_MEASURED}</span>
    ),
  },
  { key: "agent", header: "AGENT", render: (row) => row.agent_type },
  { key: "status", header: "STATUS", render: (row) => row.status ?? NOT_MEASURED },
  {
    key: "tools",
    header: "TOOLS USED",
    render: (row) => <ToolsCell runId={row.id} cell={row.toolsCell} />,
  },
  {
    key: "cost",
    header: "COST",
    render: (row) => (
      <span data-testid={`ops-cost-${row.id}`}>{costText(row.cost_usd)}</span>
    ),
  },
  {
    key: "duration",
    header: "DURATION",
    render: (row) => (
      <span data-testid={`ops-duration-${row.id}`} title={row.ended_at ?? undefined}>
        {durationText(row.duration_secs)}
      </span>
    ),
  },
];

export function OpsRunsTable({ tick, onRetry }: { tick: number; onRetry: () => void }) {
  const { data, error, loading, tools } = useOpsRuns(tick);

  const rows: (OpsRunRow & { toolsCell?: OpsToolsCell })[] =
    data === null
      ? []
      : data.items.map((row) => ({ ...row, toolsCell: tools[row.id] }));

  const truncated = data !== null && data.items.length < data.total;
  const countLine =
    data === null
      ? null
      : data.total === 0
        ? "0 runs in the ledger"
        : truncated
          ? `Showing the newest ${data.items.length} of ${data.total} runs`
          : `Showing all ${data.total}`;

  return (
    <Card
      title="Live runs"
      kicker="AGENT RUNS · NEWEST FIRST"
      action={
        countLine === null ? undefined : (
          <span className="ui-label text-ink-muted" data-testid="ops-runs-count">
            {countLine}
          </span>
        )
      }
    >
      {loading ? <ListSkeleton testId="ops-runs-loading" /> : null}

      {!loading && error !== null ? (
        <ReadError
          label="the runs ledger"
          message={error}
          onRetry={onRetry}
          retryTestId="ops-runs-retry"
        />
      ) : null}

      {!loading && error === null && data !== null ? (
        <div data-testid="ops-runs-table">
          <DataTable<(typeof rows)[number]>
            label="Live agent runs"
            columns={COLUMNS}
            rows={rows}
            rowKey={(row) => String(row.id)}
            empty={
              <p className="text-sm text-ink-muted">
                No runs recorded yet — dispatch an agent below; every run writes a ledger row here.
              </p>
            }
          />
          <p className="mt-1 text-xs text-ink-muted">
            Tools come from each run&rsquo;s own detail record: a dash with &ldquo;never
            recorded&rdquo; is missing history, while &ldquo;none&rdquo; means tools were available
            and the model called none. Duration and cost are the ledger&rsquo;s stored values.
          </p>
        </div>
      ) : null}
    </Card>
  );
}

"use client";

import { useEffect, useState } from "react";

import { RunArtifacts } from "@/components/artifacts/RunArtifacts";
import { agentLabel, absoluteStamp, NOT_MEASURED } from "@/components/dashboard/format";
import { ListSkeleton } from "@/components/finance/states";
import { errorText } from "@/components/tasks/status";
import { Card, Chip } from "@/components/ui";
import type { RunsRow } from "@/components/usage/useRuns";
import { api } from "@/lib/api";

/**
 * F-27 (wave 4) — the task page's door into the agent artifacts.
 *
 * The owner reads work from `/tasks/{id}`, but a run workspace belongs to a RUN, and the task wire
 * carries no run ids (`TaskResponse` has no such field). The honest join is the one the artifacts
 * API's own contract names: `GET /api/v1/runs?task_id=` — the measured ledger of runs for this
 * task. ONE bounded ledger read here lists them; expanding a run renders the shared `RunArtifacts`
 * panel (`GET /artifacts/{run_id}`), so the page never fans out across every run on mount.
 *
 * States kept distinct:
 *   * loading -> skeleton ("not read yet" is not "no runs");
 *   * read failure -> named in a POLITE live region with Retry (this page already owns an alert
 *     for the task read itself), never an empty list standing in for a failure;
 *   * `total: 0` -> "No run has run this task yet" — the ledger's measured zero;
 *   * runs -> each row's id/status/started plus two controls: expand its files (a read on demand)
 *     and open the full `/runs/{id}` page.
 *
 * One read on mount plus this card's Refresh. Nothing here writes.
 */

const RUNS_PAGE_LIMIT = 20;

type RunsPage = { items: RunsRow[]; total: number };

export function TaskArtifacts({ taskId }: { taskId: number }) {
  const [reloadKey, setReloadKey] = useState(0);
  const [openRun, setOpenRun] = useState<number | null>(null);
  const [data, setData] = useState<RunsPage | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    (async () => {
      try {
        const body = await api.get<RunsPage>(
          `/runs?task_id=${taskId}&limit=${RUNS_PAGE_LIMIT}`,
        );
        if (cancelled) return;
        // A 2xx body that is not the frozen `{items,total}` wrapper is a BROKEN read, named as
        // one — never rendered as "this task has no runs".
        if (
          body === null ||
          typeof body !== "object" ||
          Array.isArray(body) ||
          !Array.isArray(body.items) ||
          typeof body.total !== "number"
        ) {
          setData(null);
          setError("the server returned no runs page");
          return;
        }
        setData({ items: body.items, total: body.total });
      } catch (e: unknown) {
        if (!cancelled) {
          setData(null);
          setError(errorText(e));
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [taskId, reloadKey]);

  return (
    <Card
      data-testid="task-artifacts"
      title="Run artifacts"
      kicker="RUNS FOR THIS TASK · WHAT EACH ONE WROTE"
      action={
        <button
          type="button"
          className="link ui-label"
          data-testid="task-artifacts-refresh"
          onClick={() => {
            setOpenRun(null);
            setReloadKey((count) => count + 1);
          }}
        >
          Refresh
        </button>
      }
    >
      <p className="text-sm text-ink-muted">
        Files belong to a <strong>run</strong>, not to the task row: expand a run to read exactly
        what it wrote, or open its page. A run that wrote nothing says so — a deliverable the task
        only claims in prose was not written.
      </p>

      {loading ? <ListSkeleton testId="task-artifacts-loading" /> : null}

      {!loading && error !== null ? (
        <div aria-live="polite" className="mt-2" data-testid="task-artifacts-error">
          <p className="text-sm text-alert">Could not load this task&rsquo;s runs: {error}</p>
          <button
            type="button"
            className="link ui-label mt-1"
            data-testid="task-artifacts-retry"
            onClick={() => setReloadKey((count) => count + 1)}
          >
            Retry
          </button>
        </div>
      ) : null}

      {!loading && error === null && data !== null && data.total === 0 ? (
        <p className="mt-2 text-sm text-ink-muted" data-testid="task-artifacts-empty">
          No run has run this task yet — there are no artifacts to open. This is a measured zero
          from the runs ledger, not a failed read.
        </p>
      ) : null}

      {!loading && error === null && data !== null && data.items.length > 0 ? (
        <>
          <ul className="mt-2 space-y-2" data-testid="task-artifacts-list">
            {data.items.map((run) => {
              const expanded = openRun === run.id;
              return (
                <li key={run.id} className="border border-line bg-surface-sunk p-2">
                  <div className="flex flex-wrap items-center gap-2">
                    <button
                      type="button"
                      className="ui-label text-link text-left hover:underline"
                      aria-expanded={expanded}
                      data-testid={`task-artifact-run-${run.id}`}
                      onClick={() => setOpenRun((current) => (current === run.id ? null : run.id))}
                    >
                      {expanded ? "▾" : "▸"} run #{run.id}
                    </button>
                    <Chip variant="soft">{run.status ?? "status unknown"}</Chip>
                    <span className="ui-label text-ink-muted">{agentLabel(run.agent_type)}</span>
                    <span className="ui-label text-ink-muted">
                      started {run.started_at ? absoluteStamp(run.started_at) : NOT_MEASURED}
                    </span>
                    <a
                      className="ui-label ml-auto text-link hover:underline"
                      data-testid={`task-artifact-run-link-${run.id}`}
                      href={`/runs/${run.id}`}
                    >
                      Open run #{run.id} →
                    </a>
                  </div>
                  {expanded ? <RunArtifacts runId={run.id} bare /> : null}
                </li>
              );
            })}
          </ul>
          {data.items.length < data.total ? (
            <p className="ui-label mt-2 text-ink-muted" data-testid="task-artifacts-truncation">
              Showing the newest {data.items.length} of {data.total} runs — expand a run here or
              open its page for the rest.
            </p>
          ) : null}
        </>
      ) : null}
    </Card>
  );
}

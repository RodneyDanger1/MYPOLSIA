"use client";

import { Card } from "@/components/ui";
import { useAgentStatus } from "@/hooks/useAgentStatus";
import type { AgentStatus } from "@/lib/api";

import { relativeTime } from "./format";
import { PanelError, PanelSkeleton } from "./states";

/**
 * The left rail's status card — the live `Polsia` widget's slot, expressing the real state of this
 * workspace's agents from `GET /agents/status` (the endpoint answers one row per agent type —
 * exactly nine, even on an empty database).
 *
 * What is deliberately NOT here:
 *  - the ASCII robot mascot (an art asset this clone does not have — drawing one would be fiction);
 *  - the `AUTO MODE` / `NIGHT TASK` pills: both states ARE backed now (`GET /auto-mode`, with
 *    POST/DELETE to control it; the `night_task_*` config fields) and are controlled from the
 *    Auto Mode panel below this card and from Settings — drawing duplicate pills here would be a
 *    second, drifting control. (The old claim that nothing exposed these states was false ever
 *    since the U5 routes landed; removed in wave V7 / F-03.)
 *
 * Everything shown is read off the nine status rows: whether any agent's latest run is still
 * `running`, the newest `last_run_at`, and how many agents have tasks created today.
 */
export function PolsiaStatusCard() {
  const { statuses, loading, error } = useAgentStatus();

  return (
    <Card title="Polsia">
      {loading ? <PanelSkeleton rows={3} /> : null}
      {!loading && error ? <PanelError label="agent status" message={error} /> : null}
      {!loading && !error ? <StatusBody statuses={statuses} /> : null}
    </Card>
  );
}

function StatusBody({ statuses }: { statuses: AgentStatus[] }) {
  if (statuses.length === 0) {
    return <p className="text-sm text-ink-muted">No agent status rows were returned.</p>;
  }

  const running = statuses.filter((row) => row.last_run_status === "running");
  let latestRunAt: string | null = null;
  let latestRunMs = Number.NEGATIVE_INFINITY;
  for (const row of statuses) {
    if (!row.last_run_at) continue;
    const parsed = Date.parse(row.last_run_at);
    if (!Number.isNaN(parsed) && parsed > latestRunMs) {
      latestRunMs = parsed;
      latestRunAt = row.last_run_at;
    }
  }
  const agentsWithTasksToday = statuses.filter((row) => row.tasks_today > 0).length;
  const working = running.length > 0;

  return (
    <div className="space-y-2">
      <p className="flex items-center gap-2">
        <span
          aria-hidden="true"
          className={`inline-block h-2 w-2 rounded-full ${working ? "bg-accent" : "bg-ink-muted"}`}
        />
        <span className="ui-label font-bold text-ink">{working ? "Working" : "Idle"}</span>
      </p>
      <p className="text-sm text-ink-muted">
        {working
          ? `${running.length} of ${statuses.length} agents running now`
          : "No agent run in flight"}
      </p>
      <dl className="space-y-1 border-t border-line pt-2">
        <div className="flex items-baseline justify-between gap-3">
          <dt className="ui-label text-ink-muted">Last run</dt>
          <dd className="ui-label text-ink">
            {latestRunAt ? relativeTime(latestRunAt) : "none recorded"}
          </dd>
        </div>
        <div className="flex items-baseline justify-between gap-3">
          <dt className="ui-label text-ink-muted">Agents with tasks today</dt>
          <dd className="ui-label text-ink">
            {agentsWithTasksToday} of {statuses.length}
          </dd>
        </div>
      </dl>
    </div>
  );
}

"use client";

import { useCallback, useState } from "react";

import { OpsApprovals } from "@/components/ops/OpsApprovals";
import { OpsRunsTable } from "@/components/ops/OpsRunsTable";
import { OpsSwitches } from "@/components/ops/OpsSwitches";
import { OpsTriggers } from "@/components/ops/OpsTriggers";
import { OPS_POLL_MS, useVisibilityPoll } from "@/hooks/useVisibilityPoll";

/**
 * The owner's God Mode cockpit (V25/L5) — one surface for the questions that otherwise take five
 * pages to answer: what is running, what is waiting on me, how do I dispatch an agent, and which
 * switch is currently stopping work?
 *
 * Everything here is a THIN consumer of routes that already shipped (`/runs`, `/tasks`,
 * `/approvals`, `/agents/status`, `/agents/{type}/trigger`, `/auto-mode`, `/config`,
 * `/ops/sandbox`). No backend was widened for this page and no outcome is mocked: every control
 * calls a real handler, every failure renders the server's named refusal, and every empty/loading
 * state is its own explicit sentence.
 *
 * Polling: one tick every `OPS_POLL_MS` while the tab is VISIBLE (`useVisibilityPoll`); a hidden
 * tab issues nothing, and returning to it re-reads immediately. The header's Refresh is the manual
 * path and shares the same tick. No interval outlives the page.
 */
export default function OpsPage() {
  const [tick, setTick] = useState(0);
  const bump = useCallback(() => setTick((current) => current + 1), []);
  useVisibilityPoll(bump);

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-end gap-2 border-b border-line pb-1">
        <div className="mr-auto">
          <h1 className="text-3xl font-bold text-ink">Ops</h1>
          <p className="ui-label text-ink-muted">
            GOD MODE · owner cockpit · live every {OPS_POLL_MS / 1000} s while this tab is visible
          </p>
        </div>
        <button
          type="button"
          className="link ui-label"
          data-testid="ops-page-refresh"
          onClick={bump}
        >
          Refresh
        </button>
      </div>

      <OpsRunsTable tick={tick} onRetry={bump} />

      <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">
        <OpsApprovals tick={tick} onChanged={bump} />
        <OpsSwitches tick={tick} onRetry={bump} />
      </div>

      <OpsTriggers tick={tick} onRetry={bump} />
    </div>
  );
}

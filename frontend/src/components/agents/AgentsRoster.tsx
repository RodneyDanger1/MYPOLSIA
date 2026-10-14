"use client";

import { Chip } from "@/components/ui";
import { Button } from "@/components/ui";
import { agentLabel, relativeTime } from "@/components/dashboard/format";
import { useAgentStatus } from "@/hooks/useAgentStatus";
import { statusChipClass } from "@/components/tasks/status";
import type { AgentStatus } from "@/lib/api";

/**
 * Per-agent helper copy. Not API data — honest as long as it describes the REAL registry, and
 * audited so (Q-20: 9/9 match `task_service.VALID_AGENT_TYPES`; agents-page.md §2). A type this
 * map does not know renders NO paragraph — an unknown agent never gets a blank line (Q-20's
 * `?? ""` fix).
 */
export const AGENT_DESCRIPTIONS: Record<string, string> = {
  orchestrator: "Generates daily task plans and evening summaries",
  business_planning: "Analyzes strategy, updates KPIs, identifies opportunities",
  competitor_research: "Researches competitors and market positioning",
  social_media: "Drafts and posts tweets, monitors engagement",
  ads_management: "Manages Google and Meta ad campaigns",
  email_outreach: "Finds prospects and sends personalized cold emails",
  code_generation: "Writes code, commits to GitHub, deploys",
  customer_support: "Reads inbox and drafts customer replies",
  finance: "Monitors Stripe revenue, expenses, and alerts",
};

/**
 * The 9-row roster (agents-page.md R2). The list comes from `GET /agents/status`, which answers
 * with exactly the nine registry rows even on an empty DB — so the only "missing list" case is a
 * FAILED poll, rendered as the named failure and never as an empty "no agents" claim.
 *
 * The hook lives here (not in the page) on purpose: the page remounts this component (via `key`)
 * after a successful trigger, which is how the Q-20 "no refetch after a trigger" fix is
 * delivered without a second poller or a change to the shared hook.
 */
export function AgentsRoster({
  pending,
  confirmAgent,
  onRunRequest,
  onRunCancel,
  onRunConfirm,
}: {
  /** Per-agent in-flight map (Q-20): one slow trigger never disables another row's button. */
  pending: Record<string, boolean>;
  confirmAgent: string | null;
  onRunRequest: (agentType: string) => void;
  onRunCancel: () => void;
  onRunConfirm: (agentType: string) => void;
}) {
  const { statuses, loading, error } = useAgentStatus();

  if (loading) {
    return (
      <div aria-busy="true" className="space-y-3">
        <p className="ui-label text-ink-muted">Loading agent status…</p>
        {Array.from({ length: 9 }).map((_, index) => (
          <div key={index} className="h-20 animate-pulse border border-line bg-surface" />
        ))}
      </div>
    );
  }

  if (error) {
    // The registry is 9 fixed rows; a failed poll must never render as an empty roster.
    return (
      <p role="alert" className="border border-alert bg-surface p-3 text-sm text-ink">
        Could not load agent status: {error}
      </p>
    );
  }

  if (statuses.length === 0) {
    // Cannot happen with today's endpoint (it serves nine rows on an empty DB), but a success
    // carrying no rows must not masquerade as "no agents configured" — it is said as it is.
    return <p className="ui-label text-ink-muted">The status endpoint returned no agent rows.</p>;
  }

  return (
    <div className="space-y-3">
      {statuses.map((status) => (
        <AgentRow
          key={status.agent_type}
          status={status}
          pending={pending[status.agent_type] === true}
          confirming={confirmAgent === status.agent_type}
          disabledByOther={confirmAgent !== null && confirmAgent !== status.agent_type}
          onRunRequest={onRunRequest}
          onRunCancel={onRunCancel}
          onRunConfirm={onRunConfirm}
        />
      ))}
    </div>
  );
}

function AgentRow({
  status,
  pending,
  confirming,
  disabledByOther,
  onRunRequest,
  onRunCancel,
  onRunConfirm,
}: {
  status: AgentStatus;
  pending: boolean;
  confirming: boolean;
  disabledByOther: boolean;
  onRunRequest: (agentType: string) => void;
  onRunCancel: () => void;
  onRunConfirm: (agentType: string) => void;
}) {
  const label = agentLabel(status.agent_type);
  const description = AGENT_DESCRIPTIONS[status.agent_type];
  const runStatus = status.last_run_status;
  return (
    <article data-agent={status.agent_type} className="card-surface p-3 sm:p-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            {/* Visible text = the humanized name; `title` keeps the raw `agent_type` reachable. */}
            <h3 className="ui-label text-ink" title={status.agent_type}>
              {label}
            </h3>
            {/* Run-status token: null = no run was ever recorded -> `NEVER RUN` (Q-21's fix;
                a colour only, never a pulse). A recorded word renders as itself. */}
            <Chip variant="soft" className={statusChipClass(runStatus)}>
              {runStatus ?? "NEVER RUN"}
            </Chip>
          </div>
          {description ? <p className="mt-1 text-sm text-ink-muted">{description}</p> : null}
          <p className="ui-label mt-1 flex flex-wrap items-center gap-x-2 text-ink-muted">
            <span>LAST RUN {status.last_run_at ? relativeTime(status.last_run_at) : "NEVER"}</span>
            <span aria-hidden>·</span>
            <span>{status.tasks_today} TODAY</span>
            <span aria-hidden>·</span>
            <span>{status.tasks_total} TOTAL</span>
          </p>
        </div>
        <Button
          size="sm"
          disabled={pending || disabledByOther}
          aria-expanded={confirming}
          onClick={() => onRunRequest(status.agent_type)}
        >
          {pending ? "Triggering…" : "Run now"}
        </Button>
      </div>

      {confirming ? (
        // B21-lite confirm (§2/§3.4): names the agent and says what the click does. CANCEL issues
        // NO request. The daily-budget line is honest: the run spends from the AI-spend cap the
        // dispatch gate enforces; no price is quoted because none is measured per run.
        <div
          role="group"
          aria-label={`Confirm run for ${label}`}
          className="mt-3 border border-line-strong bg-surface-sunk p-3"
        >
          <p className="text-sm text-ink">
            Run {label} now? This creates a NEW task (source: manual) and queues it for the
            worker — it spends from today&rsquo;s AI budget. Existing tasks are untouched.
          </p>
          <div className="mt-2 flex gap-2">
            <Button
              variant="glossy"
              size="sm"
              disabled={pending}
              onClick={() => onRunConfirm(status.agent_type)}
            >
              {pending ? "Triggering…" : "Create task"}
            </Button>
            <Button size="sm" disabled={pending} onClick={onRunCancel}>
              Cancel
            </Button>
          </div>
        </div>
      ) : null}
    </article>
  );
}

import type { ReactNode } from "react";

import { NOT_MEASURED, absoluteStamp } from "@/components/dashboard/format";
import { countText, textOrDash } from "@/components/finance/format";
import type { RunDetailRow } from "@/components/runs/useRunDetail";
import { Card, Chip } from "@/components/ui";

/**
 * One run's detail body: the stored ledger row + the honest tool-activity panel.
 *
 * THE TOOL PANEL IS THE POINT OF THIS PAGE (V21/L2).  The run record stores no `tools_used` —
 * the runtime reports the list on its envelope and the persistence boundary drops it — so the
 * panel says "not recorded" and shows the two counters that ARE stored (`num_turns`, `is_error`).
 * It renders NO chip, because a chip needs a stored tool name and a stored count; there is
 * nothing here to count.  `[]` would have been the measured zero "tools were available, none
 * ran" — a different fact from "never recorded", and a lie this page refuses to tell.
 *
 * Every nullable value uses the app's shared formatters: null -> em dash, never 0 / false.
 */

/** The ledger's money shape: 4dp, so a measured sub-cent cost never collapses into `$0.00`. */
function costText(value: number | null): string {
  if (typeof value !== "number" || !Number.isFinite(value)) return NOT_MEASURED;
  return `$${value.toLocaleString("en-US", {
    minimumFractionDigits: 4,
    maximumFractionDigits: 4,
  })}`;
}

/** A duration reading: one decimal of seconds, or unmeasured. */
function durationText(value: number | null): string {
  if (typeof value !== "number" || !Number.isFinite(value)) return NOT_MEASURED;
  return `${value.toFixed(1)}s`;
}

/** The stored error flag: `null` is NOT `false` — it is "not recorded". */
function errorFlagText(value: boolean | null): string {
  if (value === null) return NOT_MEASURED;
  return value ? "error envelope seen" : "no error envelope";
}

function Field({ label, value, testId }: { label: string; value: ReactNode; testId?: string }) {
  return (
    <div className="min-w-0">
      <dt className="ui-label text-ink-muted">{label}</dt>
      <dd className="truncate text-sm text-ink" data-testid={testId}>
        {value}
      </dd>
    </div>
  );
}

export function RunDetail({ run }: { run: RunDetailRow }) {
  return (
    <div className="space-y-4">
      <Card
        title={run.task_title ?? `Run #${run.id}`}
        kicker={`RUN #${run.id}`}
        action={<Chip variant="soft">{run.status ?? "status unknown"}</Chip>}
        data-testid="run-detail-card"
      >
        <dl className="grid grid-cols-2 gap-4 md:grid-cols-3 xl:grid-cols-4">
          <Field label="AGENT" value={run.agent_type} testId="run-agent" />
          <Field label="RUN TYPE" value={textOrDash(run.run_type)} testId="run-type" />
          <Field label="ATTEMPT" value={countText(run.attempt)} testId="run-attempt" />
          <Field
            label="TASK"
            value={
              run.task_id === null ? (
                NOT_MEASURED
              ) : (
                <a className="link" href={`/tasks/${run.task_id}`}>
                  #{run.task_id}
                </a>
              )
            }
            testId="run-task"
          />
          <Field label="STARTED" value={absoluteStamp(run.started_at)} testId="run-started" />
          <Field label="FINISHED" value={absoluteStamp(run.ended_at)} testId="run-finished" />
          <Field label="DURATION" value={durationText(run.duration_secs)} testId="run-duration" />
          <Field label="TOKENS" value={countText(run.tokens_used)} testId="run-tokens" />
          <Field label="COST" value={costText(run.cost_usd)} testId="run-cost" />
          <Field label="COST SOURCE" value={textOrDash(run.cost_source)} testId="run-cost-source" />
          <Field
            label="PRICING VERSION"
            value={textOrDash(run.pricing_version)}
            testId="run-pricing-version"
          />
        </dl>
      </Card>

      <Card title="Tool activity" data-testid="run-tool-activity">
        <div className="space-y-3">
          <p className="text-sm text-ink" data-testid="run-tools-not-recorded">
            <span className="ui-label mr-2 text-ink-muted">TOOLS</span>
            Tool activity: not recorded for this run.
          </p>
          <p className="text-sm text-ink-muted">
            The runtime reports <code>tools_used</code> on its invocation envelope, but this build
            does not persist that list on the run record. No per-tool count is shown because there
            is no stored tool list to count; absence here means &ldquo;not recorded&rdquo;, never
            &ldquo;no tools ran&rdquo;.
          </p>

          <dl className="grid grid-cols-2 gap-4 md:grid-cols-3">
            <Field
              label="TURNS (STORED)"
              value={countText(run.num_turns)}
              testId="run-turns"
            />
            <Field
              label="ERROR ENVELOPE (STORED)"
              value={errorFlagText(run.is_error)}
              testId="run-error-flag"
            />
          </dl>

          <p className="text-sm text-ink-muted" data-testid="run-tools-available-note">
            Per-tool chips become available only when a future writer persists the envelope&rsquo;s
            tool list; this page will not invent one in the meantime.
          </p>
        </div>
      </Card>
    </div>
  );
}

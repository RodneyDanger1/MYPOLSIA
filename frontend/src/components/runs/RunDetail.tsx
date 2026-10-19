import type { ReactNode } from "react";

import { NOT_MEASURED, absoluteStamp } from "@/components/dashboard/format";
import { countText, textOrDash } from "@/components/finance/format";
import type { RunDetailRow } from "@/components/runs/useRunDetail";
import { Card, Chip } from "@/components/ui";

/**
 * One run's detail body: the stored ledger row + the REAL tool-activity panel.
 *
 * THE TOOL PANEL (V22/L1) renders the three states the backend now serves, and never merges them:
 *
 *   * `tools_used === null`  — NEVER RECORDED (legacy row, mock mode, tool-less run).  The honest
 *     "not recorded" sentence, with NO chip: a chip needs a stored tool name.
 *   * `tools_used === []`    — RECORDED AND NONE: "No tools were called in this run."  A measured
 *     zero, not a missing value.
 *   * names                  — one chip per tool with its COUNT (`×N`) from the stored list.  An
 *     entry that is the writer's truncation marker (leading `…`) is a NOTE, never a tool chip.
 *
 * Every nullable value uses the app's shared formatters: null -> em dash, never 0 / false.
 */

/** The writer's truncation marker starts with an ellipsis (`accounting.TOOLS_USED_TRUNCATION`). */
const TRUNCATION_PREFIX = "…";

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

function isTruncationMarker(name: string): boolean {
  return name.trimStart().startsWith(TRUNCATION_PREFIX);
}

/** Tool names in first-seen order with their occurrence count (the stored list is the truth). */
export function toolCounts(names: string[]): { name: string; count: number }[] {
  const counts = new Map<string, number>();
  for (const name of names) {
    counts.set(name, (counts.get(name) ?? 0) + 1);
  }
  return [...counts.entries()].map(([name, count]) => ({ name, count }));
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

/** The chip row for a recorded, non-empty tool list (names + counts + truncation note). */
function ToolChips({ names }: { names: string[] }) {
  const tools = toolCounts(names.filter((name) => !isTruncationMarker(name)));
  const markers = names.filter(isTruncationMarker);
  return (
    <div className="flex flex-wrap items-center gap-2" data-testid="run-tool-chips">
      {tools.map(({ name, count }) => (
        <span key={name} data-testid={`run-tool-chip-${name}`} data-tool={name}>
          <Chip variant="soft" data-testid="run-tool-chip">
            <span>{name}</span>
            <span className="ml-2 text-ink-muted" aria-label="call count">
              ×{count}
            </span>
          </Chip>
        </span>
      ))}
      {markers.length > 0 ? (
        <p className="text-sm text-ink-muted" data-testid="run-tools-truncated">
          Tool list capped: {markers.join("; ")}
        </p>
      ) : null}
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
          {run.tools_used === null ? (
            <>
              <p className="text-sm text-ink" data-testid="run-tools-not-recorded">
                <span className="ui-label mr-2 text-ink-muted">TOOLS</span>
                Tool activity: not recorded for this run.
              </p>
              <p className="text-sm text-ink-muted">
                This run&rsquo;s record carries no stored tool list — no chip is shown, because a
                chip needs a recorded tool name and a count. Absence here means &ldquo;not
                recorded&rdquo;, never &ldquo;no tools ran&rdquo;.
              </p>
            </>
          ) : run.tools_used.length === 0 ? (
            <p className="text-sm text-ink" data-testid="run-tools-none">
              <span className="ui-label mr-2 text-ink-muted">TOOLS</span>
              No tools were called in this run.
            </p>
          ) : (
            <ToolChips names={run.tools_used} />
          )}

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
        </div>
      </Card>
    </div>
  );
}

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
 *
 * F-36 (wave 5) — THE INTERRUPTION MARKERS.  `cost_scope` / `interrupted_reason` are the F-26
 * fields the stale-run sweep stores when it closes a run whose worker died (SV-W4-04 put them on
 * `GET /runs/{id}`; nothing consumed them until now).  Without a consumer, run 314 read
 * `CANCELLED · FINISHED 7:51 PM · COST —` — the sweep time looked like a completion and an
 * unmeasured cost looked like "not measured yet".  An interrupted row now gets a named banner
 * (`interrupted · unmeasured cost · E_RUN_STALE`) that says the FINISHED stamp is the ledger's
 * closing time, states what the cost scope covers (`unmeasured` = unknown, never a zero;
 * `in_flight_partial` = a lower bound measured up to the kill), and renders an unknown scope/reason
 * VERBATIM with "not one this build knows".  A settled row keeps no banner; both marker fields are
 * still shown as their own rows so absence is the em dash, never a guess.
 */

/** The cost-scope vocabulary the F-26 closer writes (`task_service.RUN_COST_SCOPE_KEY`). */
const COST_SCOPE_COPY: Record<string, { headline: string; sentence: string }> = {
  unmeasured: {
    headline: "unmeasured cost",
    sentence: "Its cost was never measured — the COST row is unknown, not a zero.",
  },
  in_flight_partial: {
    headline: "partial cost (lower bound)",
    sentence:
      "Its cost is what the live run measured up to the interruption — a lower bound on what was spent, not the run's total.",
  },
};

/** The one reason code this build can explain (`maintenance.STALE_RUN_REASON_CODE`). */
const STALE_RUN_REASON = "E_RUN_STALE";
const STALE_RUN_SENTENCE =
  "The stale-run sweep closed this run: its worker stopped reporting, so it did not finish on its own.";
const CLOSING_TIME_SENTENCE =
  "The FINISHED time above is the ledger's closing time, not a completion.";

type InterruptionRead = { headline: string; detail: string };

/**
 * F-36: the interruption banner from the two F-26 markers, or `null` when the row carries neither
 * (a settled run, or a server that predates SV-W4-04).  Unknown values are quoted verbatim and
 * labelled unknown — this function never invents an explanation for a code it does not know.
 */
export function interruptionRead(run: RunDetailRow): InterruptionRead | null {
  const scope = run.cost_scope ?? null;
  const reason = run.interrupted_reason ?? null;
  if (scope === null && reason === null) return null;

  const known = scope === null ? undefined : COST_SCOPE_COPY[scope];
  const reasonText = reason ?? "no interruption reason recorded";
  const headline =
    known !== undefined
      ? `interrupted · ${known.headline} · ${reasonText}`
      : scope !== null
        ? `interrupted · cost scope “${scope}” · ${reasonText}`
        : `interrupted · cost scope not recorded · ${reasonText}`;

  const parts: string[] = [
    reason === null
      ? "No interruption reason code was recorded, so why it stopped is unknown."
      : reason === STALE_RUN_REASON
        ? STALE_RUN_SENTENCE
        : `The interruption code “${reason}” is not one this build knows; treat the run as not completed.`,
    CLOSING_TIME_SENTENCE,
  ];
  parts.push(
    scope === null
      ? "The row carries no cost scope, so the COST reading cannot be attributed to a full or partial measurement."
      : known !== undefined
        ? known.sentence
        : `The recorded cost scope “${scope}” is not one this build knows; do not read COST as a settled total.`,
  );
  return { headline, detail: parts.join(" ") };
}

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
  const interruption = interruptionRead(run);
  return (
    <div className="space-y-4">
      <Card
        title={run.task_title ?? `Run #${run.id}`}
        kicker={`RUN #${run.id}`}
        action={<Chip variant="soft">{run.status ?? "status unknown"}</Chip>}
        data-testid="run-detail-card"
      >
        {interruption !== null ? (
          <div
            className="mb-3 border border-alert bg-surface p-2"
            data-testid="run-interruption"
          >
            <p className="ui-label text-alert" data-testid="run-interruption-headline">
              {interruption.headline}
            </p>
            <p className="mt-1 text-sm text-ink" data-testid="run-interruption-detail">
              {interruption.detail}
            </p>
          </div>
        ) : null}
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
          {/* F-36: the F-26 markers as their own readings — absence is the em dash, not a guess. */}
          <Field label="COST SCOPE" value={textOrDash(run.cost_scope)} testId="run-cost-scope" />
          <Field
            label="INTERRUPTION"
            value={textOrDash(run.interrupted_reason)}
            testId="run-interrupted-reason"
          />
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

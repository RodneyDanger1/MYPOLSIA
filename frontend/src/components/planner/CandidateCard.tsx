"use client";

import { absoluteStamp, NOT_MEASURED } from "@/components/dashboard/format";
import { Button, Chip } from "@/components/ui";
import type { PlannerCandidate, PlannerGates, PlannerRubric } from "@/components/planner/usePlanner";

/**
 * One candidate business, in the shape the planner's propose endpoint answers (plan §9 B1).
 *
 * What the card refuses to do:
 *  - hide a gated candidate. A `cm_positive: false` candidate is STILL rendered (the endpoint's own
 *    rule), with the gate chip `CM ≤ 0 — blocked` — a GATE, so it wears the soft chip + amber label
 *    the app's status vocabulary already uses for "waiting on a decision" (`components/tasks/
 *    status.ts`), not `chip-alert` (which this app reserves for destructive markers). It is never a
 *    red error state and never silently dropped;
 *  - render an unmeasured gate as `false`. `budget_fit`/`hours_fit` are `bool | null` on the wire;
 *    null says so;
 *  - print a claim without its source. Every claim row carries its own link to the fetched page and
 *    the verbatim quote; a claim the model stated without a fetch is stripped by the backend and
 *    only its COUNT arrives (`claims_unverified`), which the page states out loud.
 *
 * The rubric is the backend's Python-computed 0-5 × 6 axes + total 0-30 (`planner_service.py`); the
 * bars are a display of that number over its own maximum — nothing is rescaled or re-scored here.
 */

const AXES: ReadonlyArray<{ key: keyof PlannerRubric; label: string }> = [
  { key: "demand", label: "Demand" },
  { key: "margin", label: "Margin" },
  { key: "competition", label: "Competition" },
  { key: "agent_fit", label: "Agent fit" },
  { key: "time_to_first_dollar", label: "Time to first dollar" },
  { key: "owner_fit", label: "Owner fit" },
];

const AXIS_MAX = 5;
const TOTAL_MAX = 30;

/** `5/5`, or the not-measured marker — never `0/5` for an axis the service did not score. */
function axisText(value: number | null): string {
  return typeof value === "number" && Number.isFinite(value) ? `${value}/${AXIS_MAX}` : NOT_MEASURED;
}

function barWidth(value: number | null): string {
  if (typeof value !== "number" || !Number.isFinite(value)) return "0%";
  const clamped = Math.max(0, Math.min(AXIS_MAX, value));
  return `${(clamped / AXIS_MAX) * 100}%`;
}

/** The gate vocabulary. `state` is tri-valued on purpose: unknown is its own answer. */
function gateChip(label: string, passedText: string, blockedText: string, value: boolean | null) {
  if (value === null) {
    return (
      <Chip variant="soft" className="text-ink-muted" data-testid={`gate-${label}`}>
        {`${label === "cm" ? "CM" : label === "budget" ? "Budget" : "Hours"} — not measured`}
      </Chip>
    );
  }
  return value ? (
    <Chip variant="soft" className="text-green-800" data-testid={`gate-${label}`}>
      {passedText}
    </Chip>
  ) : (
    <Chip variant="soft" className="text-amber-800" data-testid={`gate-${label}`}>
      {blockedText}
    </Chip>
  );
}

function Gates({ gates }: { gates: PlannerGates }) {
  return (
    <div className="mt-2 flex flex-wrap items-center gap-2">
      {gateChip("cm", "CM > 0 — gate passed", "CM ≤ 0 — blocked", gates.cm_positive)}
      {gateChip("budget", "Budget — gate passed", "Budget — blocked", gates.budget_fit)}
      {gateChip("hours", "Hours — gate passed", "Hours — blocked", gates.hours_fit)}
    </div>
  );
}

function RubricRows({ rubric }: { rubric: PlannerRubric }) {
  return (
    <div className="mt-2">
      <dl className="space-y-1">
        {AXES.map(({ key, label }) => (
          <div key={key} className="flex items-center gap-2" data-testid={`rubric-${key}`}>
            <dt className="ui-label w-40 shrink-0 text-ink-muted">{label}</dt>
            <dd className="ui-label w-10 shrink-0 text-right text-ink">{axisText(rubric[key])}</dd>
            <div className="h-1.5 flex-1 bg-surface-strong" aria-hidden="true">
              <div className="h-1.5 bg-band" style={{ width: barWidth(rubric[key]) }} />
            </div>
          </div>
        ))}
      </dl>
      <p className="ui-label mt-1 text-ink" data-testid="rubric-total">
        Total{" "}
        {typeof rubric.total === "number" && Number.isFinite(rubric.total)
          ? `${rubric.total}/${TOTAL_MAX}`
          : NOT_MEASURED}{" "}
        of {TOTAL_MAX}
      </p>
    </div>
  );
}

function priceBandText(band: [number, number] | null): string {
  if (!band || band.length !== 2) return NOT_MEASURED;
  const [lo, hi] = band;
  const asMoney = (value: number) =>
    typeof value === "number" && Number.isFinite(value) ? `$${value.toLocaleString("en-US")}` : NOT_MEASURED;
  return `${asMoney(lo)}–${asMoney(hi)}`;
}

export function CandidateCard({
  candidate,
  confirming,
  onConfirm,
}: {
  candidate: PlannerCandidate;
  confirming: boolean;
  onConfirm: (candidate: PlannerCandidate) => void;
}) {
  return (
    <article className="card-surface px-3 py-3" data-testid={`candidate-${candidate.id}`}>
      <header className="flex items-baseline gap-2">
        <h3 className="mr-auto text-base font-bold text-ink">{candidate.name}</h3>
        <Chip variant="dark">{candidate.channel}</Chip>
      </header>
      <p className="mt-1 text-sm text-ink">{candidate.one_liner}</p>
      <p className="ui-label mt-1 text-ink-muted">
        Buyer {candidate.buyer || NOT_MEASURED} · Price band {priceBandText(candidate.price_band)} ·{" "}
        {candidate.unit_cost_sketch || NOT_MEASURED}
      </p>

      <Gates gates={candidate.gates} />
      <RubricRows rubric={candidate.rubric} />

      <h4 className="ui-label mt-3 text-ink-muted">Claims, with the page each one came from</h4>
      {candidate.claims.length === 0 ? (
        <p className="mt-1 text-sm text-ink-muted" data-testid="claims-empty">
          No cited claim survived for this candidate — the numbers above stand on the rubric only.
        </p>
      ) : (
        <ul className="mt-1 space-y-2" data-testid="claims">
          {candidate.claims.map((claim, index) => (
            <li key={`${claim.source_url}-${index}`} className="text-sm">
              <p className="text-ink">{claim.claim}</p>
              {claim.quote ? <blockquote className="text-ink-muted">“{claim.quote}”</blockquote> : null}
              <p className="mt-0.5 flex flex-wrap items-center gap-2">
                <a
                  href={claim.source_url}
                  className="link text-xs"
                  target="_blank"
                  rel="noreferrer"
                  title={claim.source_url}
                >
                  {claim.source_url}
                </a>
                {claim.fetched_at ? (
                  <span className="ui-label text-ink-muted">{absoluteStamp(claim.fetched_at)}</span>
                ) : null}
                {claim.verified === false ? (
                  <Chip variant="soft" className="text-amber-800">
                    unverified
                  </Chip>
                ) : null}
              </p>
            </li>
          ))}
        </ul>
      )}

      <div className="mt-3">
        <Button
          variant="glossy"
          size="sm"
          disabled={confirming}
          onClick={() => onConfirm(candidate)}
          data-testid={`confirm-${candidate.id}`}
        >
          {confirming ? "Confirming…" : "Confirm this idea"}
        </Button>
      </div>
    </article>
  );
}

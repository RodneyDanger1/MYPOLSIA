"use client";

import { absoluteStamp } from "@/components/dashboard/format";
import { CandidateCard } from "@/components/planner/CandidateCard";
import { FirstDollarChecklist } from "@/components/planner/FirstDollarChecklist";
import { usePlanner, type ConfirmedIdea } from "@/components/planner/usePlanner";
import { Card } from "@/components/ui";

/**
 * Planner — the business core's front door (wave V9, plan §9 B1/B5).
 *
 * The page owns three reads/writes and nothing else: `GET /planner/status` on mount, then
 * `POST /planner/propose` and `POST /planner/confirm` on the two buttons. Confirm's contract is the
 * load-bearing one for this surface: it writes `company_config.confirmed_idea` and answers
 * `{confirmed: true}`. It creates no task, starts no run and sends nothing — so the page says what
 * was SAVED and never what will happen next. There is no "run it" affordance here, and no code path
 * that would invent one.
 *
 * Every region keeps the app's three honest states: not read yet (a skeleton), failed (the failure
 * NAMED with the server's own words through `ApiError.display`), answered (the real values, with
 * null rendered as null). The empty state is a teaching state, not a marketing one: three lines,
 * one per step of the flow.
 */

function PlannedIdea({ confirmed }: { confirmed: ConfirmedIdea }) {
  const name = typeof confirmed.name === "string" && confirmed.name !== "" ? confirmed.name : null;
  const id = typeof confirmed.candidate_id === "string" ? confirmed.candidate_id : null;
  const oneLiner = typeof confirmed.one_liner === "string" ? confirmed.one_liner : null;
  const confirmedAt = typeof confirmed.confirmed_at === "string" ? confirmed.confirmed_at : null;

  return (
    <Card kicker="Confirmed idea" title={name ?? id ?? "an idea is confirmed"} data-testid="planner-confirmed">
      {oneLiner ? <p className="text-sm text-ink">{oneLiner}</p> : null}
      <dl className="mt-2 space-y-1">
        <div className="flex items-baseline justify-between gap-3">
          <dt className="ui-label text-ink-muted">Candidate</dt>
          <dd className="ui-label text-ink">{id ?? "—"}</dd>
        </div>
        <div className="flex items-baseline justify-between gap-3">
          <dt className="ui-label text-ink-muted">Saved</dt>
          <dd className="ui-label text-ink">{confirmedAt ? absoluteStamp(confirmedAt) : "date unknown"}</dd>
        </div>
      </dl>
      <p className="mt-2 text-sm text-ink-muted">
        This is the idea brief the morning plan reads. Nothing was scheduled when it was saved, and
        nothing runs because of it — dispatch is still yours.
      </p>
    </Card>
  );
}

function FlowTeaching() {
  return (
    <Card kicker="Nothing confirmed yet" title="How this works" data-testid="planner-flow">
      <ol className="space-y-2">
        <li className="text-sm text-ink" data-testid="planner-flow-step">
          <span className="ui-label mr-1 text-ink-muted">1 Propose</span> — the planner drafts 3–5
          candidate businesses from web research; every claim it keeps carries the page it was
          fetched from.
        </li>
        <li className="text-sm text-ink" data-testid="planner-flow-step">
          <span className="ui-label mr-1 text-ink-muted">2 Review</span> — the rubric scores each
          axis, the gates mark what is blocked, and the claims list shows what was actually fetched
          before you trust a number.
        </li>
        <li className="text-sm text-ink" data-testid="planner-flow-step">
          <span className="ui-label mr-1 text-ink-muted">3 Confirm</span> — confirming saves one
          idea to the company config as the idea brief. Nothing starts running.
        </li>
      </ol>
    </Card>
  );
}

export default function PlannerPage() {
  const planner = usePlanner();
  const candidates = planner.proposal?.candidates ?? null;

  return (
    <div className="space-y-4">
      <div className="flex items-end gap-2 border-b border-line pb-1">
        <div className="mr-auto">
          <h1 className="text-3xl font-bold text-ink">Planner</h1>
          {planner.status !== null ? (
            <p className="ui-label text-ink-muted" data-testid="planner-last-proposed">
              Last proposed{" "}
              {planner.status.last_proposed_at
                ? absoluteStamp(planner.status.last_proposed_at)
                : "never from this deployment"}
            </p>
          ) : null}
        </div>
        <button
          type="button"
          className="btn btn-bevel"
          data-testid="planner-propose"
          disabled={planner.proposing}
          onClick={planner.propose}
        >
          {planner.proposing ? "Proposing…" : "Propose candidates"}
        </button>
      </div>

      {planner.statusLoading ? (
        <div className="space-y-2" aria-busy="true">
          <div className="h-4 w-2/3 animate-pulse bg-surface-strong" />
          <div className="h-4 w-1/2 animate-pulse bg-surface-strong" />
        </div>
      ) : null}

      {planner.statusError !== null ? (
        <p role="alert" className="text-sm text-alert" data-testid="planner-status-error">
          Could not load the planner status: {planner.statusError}
        </p>
      ) : null}

      {planner.status !== null && planner.status.confirmed !== null ? (
        <PlannedIdea confirmed={planner.status.confirmed} />
      ) : null}

      {planner.status !== null && planner.status.confirmed === null ? <FlowTeaching /> : null}

      {planner.proposing ? (
        <p className="text-sm text-ink-muted" data-testid="planner-proposing">
          Proposing — this runs web research and one model call. Nothing is created until you
          confirm an idea.
        </p>
      ) : null}

      {planner.proposeError !== null ? (
        <p role="alert" className="text-sm text-alert" data-testid="planner-propose-error">
          {planner.proposeError}
        </p>
      ) : null}

      {planner.confirmError !== null ? (
        <p role="alert" className="text-sm text-alert" data-testid="planner-confirm-error">
          {planner.confirmError}
        </p>
      ) : null}

      {planner.confirmedNote !== null ? (
        <p className="text-sm text-ink" data-testid="planner-confirmed-note">
          {planner.confirmedNote}
        </p>
      ) : null}

      {candidates !== null ? (
        <section className="space-y-3" data-testid="planner-candidates">
          <h2 className="ui-label text-ink-muted">
            {candidates.length} candidate{candidates.length === 1 ? "" : "s"}
          </h2>
          {planner.proposal?.claims_unverified !== null &&
          planner.proposal !== null &&
          (planner.proposal.claims_unverified ?? 0) > 0 ? (
            <p className="text-sm text-ink-muted" data-testid="planner-claims-unverified">
              {planner.proposal.claims_unverified} claims were dropped — the model stated them
              without a fetched source.
            </p>
          ) : null}
          {candidates.map((candidate) => (
            <CandidateCard
              key={candidate.id}
              candidate={candidate}
              confirming={planner.confirmingId === candidate.id}
              onConfirm={planner.confirm}
            />
          ))}
        </section>
      ) : null}

      <FirstDollarChecklist />
    </div>
  );
}

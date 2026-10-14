"use client";

import { useCallback, useEffect, useState } from "react";

import { api } from "@/lib/api";

/**
 * The planner's wire shapes and its one read/write hook (wave V9 / plan §9 B1's FROZEN CONTRACT).
 *
 *   POST /api/v1/planner/propose  {}             -> 201 {candidates: [Candidate], claims_unverified}
 *   POST /api/v1/planner/confirm  {candidate_id} -> 200 {confirmed: true}
 *   GET  /api/v1/planner/status                  -> {confirmed: {...}|null, last_proposed_at: str|null}
 *
 * Null law, the same one every money/gate surface keeps: a field the server did not measure stays
 * null and renders as "not measured" — a gate is NEVER coerced from null to `false` (a gate that
 * was not evaluated is not a gate that failed), and a claim that was not verified is not a claim
 * that was refuted. `claims_unverified` is the endpoint's own count of claims the model stated
 * WITHOUT a fetched source (B1 strips them); it renders as a stated number, not as a silent drop.
 *
 * NOTHING here spends: the only two writes are propose and confirm, and confirm's own contract says
 * it writes the company config and nothing else. There is no code path in this hook that touches
 * tasks, runs, sends or approvals.
 */

export type PlannerClaim = {
  claim: string;
  source_url: string;
  fetched_at: string | null;
  quote: string | null;
  verified: boolean | null;
};

export type PlannerRubric = {
  demand: number | null;
  margin: number | null;
  competition: number | null;
  agent_fit: number | null;
  time_to_first_dollar: number | null;
  owner_fit: number | null;
  total: number | null;
};

export type PlannerGates = {
  cm_positive: boolean | null;
  budget_fit: boolean | null;
  hours_fit: boolean | null;
};

export type PlannerCandidate = {
  id: string;
  name: string;
  one_liner: string;
  buyer: string;
  price_band: [number, number] | null;
  channel: string;
  unit_cost_sketch: string;
  rubric: PlannerRubric;
  gates: PlannerGates;
  claims: PlannerClaim[];
};

/** The confirmed idea as the status endpoint serves it: the company config's `confirmed_idea`
 *  JSON. Its inner shape is the backend's to define, so every field is optional here and the page
 *  renders whatever it actually got — never a placeholder for a field that is not on the wire. */
export type ConfirmedIdea = {
  candidate_id?: string;
  name?: string;
  one_liner?: string;
  confirmed_at?: string;
} & Record<string, unknown>;

export type PlannerStatus = {
  confirmed: ConfirmedIdea | null;
  last_proposed_at: string | null;
};

export type Proposal = {
  candidates: PlannerCandidate[];
  claims_unverified: number | null;
};

/** `ApiError.display` when the error carries one (read structurally, so a suite that mocks
 *  `@/lib/api` without the class cannot crash the renderer); otherwise the error's own message. */
export function errorText(e: unknown): string {
  const display = (e as { display?: unknown } | null | undefined)?.display;
  if (typeof display === "string" && display !== "") return display;
  return e instanceof Error ? e.message : String(e);
}

export type PlannerState = {
  /** The last successful status body; null while it has not answered. */
  status: PlannerStatus | null;
  statusError: string | null;
  statusLoading: boolean;
  /** The last successful proposal; null until one answered. */
  proposal: Proposal | null;
  proposing: boolean;
  proposeError: string | null;
  /** The candidate id currently being confirmed, so the card can disable its own button. */
  confirmingId: string | null;
  confirmError: string | null;
  /** What the LAST confirm saved, in the page's own words: the confirmed candidate's name plus the
   *  explicit statement that nothing was scheduled. Cleared when a new action starts. */
  confirmedNote: string | null;
  propose: () => void;
  confirm: (candidate: PlannerCandidate) => void;
};

export function usePlanner(): PlannerState {
  const [status, setStatus] = useState<PlannerStatus | null>(null);
  const [statusError, setStatusError] = useState<string | null>(null);
  const [proposal, setProposal] = useState<Proposal | null>(null);
  const [proposing, setProposing] = useState(false);
  const [proposeError, setProposeError] = useState<string | null>(null);
  const [confirmingId, setConfirmingId] = useState<string | null>(null);
  const [confirmError, setConfirmError] = useState<string | null>(null);
  const [confirmedNote, setConfirmedNote] = useState<string | null>(null);

  /** One status read; `reload` re-issues it after a confirm (the saved idea is the SERVER's answer,
   *  not this component's optimism). */
  const readStatus = useCallback(async () => {
    try {
      const body = await api.get<PlannerStatus>("/planner/status");
      if (body === null || body === undefined) {
        setStatusError("the server returned an empty body");
        return;
      }
      setStatus(body);
      setStatusError(null);
    } catch (e: unknown) {
      setStatusError(errorText(e));
    }
  }, []);

  useEffect(() => {
    void readStatus();
  }, [readStatus]);

  const propose = useCallback(async () => {
    setProposing(true);
    setProposeError(null);
    setConfirmedNote(null);
    setConfirmError(null);
    try {
      const body = await api.post<Proposal>("/planner/propose", {});
      if (body === null || body === undefined || !Array.isArray(body.candidates)) {
        setProposal(null);
        setProposeError("the proposal arrived without any candidates");
        return;
      }
      setProposal({ candidates: body.candidates, claims_unverified: body.claims_unverified ?? null });
    } catch (e: unknown) {
      setProposal(null);
      setProposeError(errorText(e));
    } finally {
      setProposing(false);
    }
  }, []);

  const confirm = useCallback(
    async (candidate: PlannerCandidate) => {
      setConfirmingId(candidate.id);
      setConfirmError(null);
      setConfirmedNote(null);
      try {
        const body = await api.post<{ confirmed: boolean }>("/planner/confirm", {
          candidate_id: candidate.id,
        });
        if (body?.confirmed !== true) {
          // A 2xx that does not say `confirmed: true` is NOT a save — say exactly what came back.
          setConfirmError("the server answered without confirming the idea");
          return;
        }
        setConfirmedNote(
          `Saved: ${candidate.name} is the confirmed idea in the company config. ` +
            "Nothing was scheduled, no task was created, and no agent will run from this on its own.",
        );
        await readStatus();
      } catch (e: unknown) {
        setConfirmError(errorText(e));
      } finally {
        setConfirmingId(null);
      }
    },
    [readStatus],
  );

  return {
    status,
    statusError,
    statusLoading: status === null && statusError === null,
    proposal,
    proposing,
    proposeError,
    confirmingId,
    confirmError,
    confirmedNote,
    propose: () => void propose(),
    confirm: (candidate) => void confirm(candidate),
  };
}

"use client";

import { useCallback, useEffect, useState } from "react";

import { Card } from "@/components/ui";
import { api } from "@/lib/api";

/** `ApiError.display` when present (structural read — no class dependency), else the error message. */
function readErrorText(e: unknown): string {
  const display = (e as { display?: unknown } | null | undefined)?.display;
  if (typeof display === "string" && display !== "") return display;
  return e instanceof Error ? e.message : String(e);
}

/**
 * The first-dollar checklist (plan §9 B5 + its v1.1 renewal clocks).
 *
 * Every step carries the two things a checklist usually omits: its REAL cost and the page that cost
 * came from. The rows are PLAYBOOK §1's own readings (u6 research digest, fetched live 2026-10-14):
 * the SBA's no-registration rule for a sole prop under your legal name, the IRS's free-EIN warning,
 * the SBA's bank-account advice, and the renewal clocks §3.1 asks for — the MN LLC annual renewal
 * (MN SOS: "Annual Renewal | $0 | $0 | $0"), the insurance review and the subscription audit. Where
 * the playbook prices nothing, the row SAYS so instead of inventing a number.
 *
 * State lives on the company row: `checklist_state` rides the existing `PUT /api/v1/config`
 * (additive — the field is already in the contract, `backend/app/schemas/company.py:69,136`), and
 * the shape this component writes is
 *
 *     checklist_state = { "<step-id>": { "done": true, "done_at": "<iso>" } }
 *
 * with an un-ticked step ABSENT rather than stored false, so the stored object only ever holds what
 * actually happened. A save that fails is named on screen and the box is put BACK — a tick the
 * server never stored is a lie, and `PUT /config` returning an error must not leave one behind.
 */

type ChecklistStep = {
  id: string;
  /** The checkbox's accessible name. */
  label: string;
  detail: string;
  /** The cost, in the playbook's own words — a real fee, or an explicit "not priced". */
  cost: string;
  /** The playbook section this row is read from. */
  sourceLabel: string;
  sourceUrl: string;
  /** The renewal clocks (§3.1) are marked, because they are the ones that recur on a calendar. */
  renewal?: boolean;
};

const STEPS: readonly ChecklistStep[] = [
  {
    id: "structure",
    label: "Pick the structure — sole prop is the honest default",
    detail:
      "SBA: doing business under your own legal name needs no registration anywhere; take the LLC " +
      "when a client contract, a regulated service or real profit makes the liability separation worth it.",
    cost: "$0 to start as a sole prop — an MN LLC costs $135 by mail / $155 online",
    sourceLabel: "PLAYBOOK §1.3 — SBA, Register your business",
    sourceUrl: "https://legacy.sba.gov/business-guide/launch-your-business/register-your-business",
  },
  {
    id: "ein",
    label: "Get an EIN (free, from the IRS only)",
    detail:
      "The IRS tool issues one in minutes; an EIN is what keeps business and personal money separable.",
    cost: "$0 — IRS: “You never have to pay a fee for an EIN.”",
    sourceLabel: "PLAYBOOK §1.4 — IRS, Get an EIN",
    sourceUrl:
      "https://www.irs.gov/businesses/small-businesses-self-employed/get-an-employer-identification-number",
  },
  {
    id: "bank-account",
    label: "Open a business bank account",
    detail:
      "SBA: start one as soon as you accept or spend business money — separation is what makes the " +
      "books (and a Schedule C) sane.",
    cost: "no fee is priced in the playbook — check the bank's own terms",
    sourceLabel: "PLAYBOOK §1.4 — SBA, Open a business bank account",
    sourceUrl: "https://legacy.sba.gov/business-guide/launch-your-business/open-business-bank-account",
  },
  {
    id: "mn-llc-renewal",
    label: "File the MN LLC annual renewal (renewal clock)",
    detail:
      "Once every calendar year, and the state dissolves an entity that misses it; reinstatement is a " +
      "second fee on top.",
    cost:
      "$0 while the entity is active and in good standing — MN SOS fee schedule: “Annual Renewal | $0 | " +
      "$0 | $0”; retroactive reinstatement costs $65 by mail / $85 online",
    sourceLabel: "PLAYBOOK §1.3 — MN Secretary of State, Business Filing and Certification Fee Schedule",
    sourceUrl: "https://www.sos.mn.gov/business-liens/business-filing-and-certification-fee-schedule/",
    renewal: true,
  },
  {
    id: "insurance-review",
    label: "Review insurance (renewal clock)",
    detail:
      "DEED's launch order puts licenses/permits + insurance at step 4; the annual cadence is where " +
      "you re-check the cover against what the business actually does now.",
    cost: "no fee is priced in the playbook — check your carrier's own terms",
    sourceLabel: "PLAYBOOK §1.3 — MN DEED, 5 Steps to Start your Business in Minnesota (step 4)",
    sourceUrl:
      "https://mn.gov/deed/dobusiness/doing-business-here/grow-your-business/five-steps.jsp",
    renewal: true,
  },
  {
    id: "subscription-audit",
    label: "Audit every subscription/tool you still pay for (renewal clock)",
    detail:
      "The playbook's own annual cadence row: re-check each subscription and tool you are still " +
      "paying for, and only buy software after the first paying customer.",
    cost: "$0 to run — the playbook prices no savings here; §1.2's rule is to not buy tools before the first paying customer",
    sourceLabel:
      "PLAYBOOK §3.1 annual cadence (estimate — judgment) + §1.2, cited source: SBA, Manage Your Cash Flow",
    sourceUrl: "https://sba.gov/tools/content/manage-your-cash-flow",
    renewal: true,
  },
];

/** The company row's checklist field, exactly as `GET /config` serves it. Declared locally because
 *  the settings page's own `SettingsConfig` (another lane's file) does not carry the field. */
type ChecklistState = Record<string, { done?: boolean; done_at?: string | null } | undefined>;

type ConfigRow = { checklist_state?: ChecklistState | null };

export function FirstDollarChecklist() {
  const [state, setState] = useState<ChecklistState | null>(null);
  const [readError, setReadError] = useState<string | null>(null);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [savingId, setSavingId] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    api
      .get<ConfigRow>("/config")
      .then((row) => {
        if (cancelled) return;
        setState(row?.checklist_state ?? {});
      })
      .catch((e: unknown) => {
        if (cancelled) return;
        setReadError(readErrorText(e));
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const toggle = useCallback(
    async (step: ChecklistStep, done: boolean) => {
      if (state === null) return;
      setSaveError(null);
      setSavingId(step.id);
      const previous = state;
      const next: ChecklistState = { ...state };
      if (done) {
        next[step.id] = { done: true, done_at: new Date().toISOString() };
      } else {
        delete next[step.id];
      }
      setState(next); // optimistic — put back below if the server says no
      try {
        await api.put<ConfigRow>("/config", { checklist_state: next });
      } catch (e: unknown) {
        setState(previous);
        setSaveError(readErrorText(e));
      } finally {
        setSavingId(null);
      }
    },
    [state],
  );

  if (readError !== null) {
    return (
      <Card title="First dollar" data-testid="first-dollar-checklist">
        <p role="alert" className="text-sm text-alert" data-testid="checklist-read-error">
          Could not load the checklist state: {readError}
        </p>
        <p className="mt-1 text-sm text-ink-muted">
          The boxes would have nowhere to store a tick, so none are drawn — nobody should be able to
          tick a step whose state cannot be saved.
        </p>
      </Card>
    );
  }

  return (
    <Card title="First dollar" data-testid="first-dollar-checklist">
      <p className="text-sm text-ink-muted">
        The steps between here and the first paying customer, each with the real fee the playbook
        fetched and the page it came from.
      </p>

      {state === null ? (
        <div className="mt-2 space-y-2" aria-busy="true" data-testid="checklist-loading">
          {Array.from({ length: 3 }).map((_, index) => (
            <div key={index} className="h-4 animate-pulse bg-surface-strong" />
          ))}
        </div>
      ) : (
        <ul className="mt-2 space-y-3">
          {STEPS.map((step) => {
            const done = state[step.id]?.done === true;
            const inputId = `checklist-${step.id}`;
            return (
              <li key={step.id} data-testid={`checklist-step-${step.id}`} className="flex items-start gap-2">
                <input
                  id={inputId}
                  type="checkbox"
                  className="mt-1"
                  checked={done}
                  disabled={savingId !== null}
                  onChange={(event) => void toggle(step, event.target.checked)}
                />
                <div>
                  <label htmlFor={inputId} className="text-sm font-bold text-ink">
                    {step.label}
                  </label>
                  {step.renewal ? (
                    <span className="ui-label ml-2 text-ink-muted">renewal clock</span>
                  ) : null}
                  <p className="text-sm text-ink-muted">{step.detail}</p>
                  <p className="ui-label text-ink">{step.cost}</p>
                  <p className="text-xs text-ink-muted">
                    {step.sourceLabel} —{" "}
                    <a
                      href={step.sourceUrl}
                      className="link"
                      target="_blank"
                      rel="noreferrer"
                      title={step.sourceUrl}
                    >
                      {step.sourceUrl}
                    </a>
                  </p>
                  {done && state[step.id]?.done_at ? (
                    <p className="ui-label text-ink-muted">Done {state[step.id]?.done_at}</p>
                  ) : null}
                </div>
              </li>
            );
          })}
        </ul>
      )}

      {saveError !== null ? (
        <p role="alert" className="mt-2 text-sm text-alert" data-testid="checklist-save-error">
          Could not save the checklist state: {saveError}
        </p>
      ) : null}
    </Card>
  );
}

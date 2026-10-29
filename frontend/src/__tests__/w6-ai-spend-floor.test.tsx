import { render, screen } from "@testing-library/react";

// The same harness FinancePage.test.tsx uses: keep the REAL module exports and replace only
// `api.get` (the page distinguishes a named failure through the real ApiError class).
jest.mock("@/lib/api", () => {
  const actual = jest.requireActual("@/lib/api");
  return { ...actual, api: { get: jest.fn() } };
});

import { api, type FinanceSummary } from "@/lib/api";
import FinancePage from "@/app/finance/page";
import { aiSpendNote, aiSpendValue } from "@/components/finance/summary";

const mockApiGet = api.get as jest.MockedFunction<typeof api.get>;

/**
 * F-W5-TS-1 (wave 6) — the AI spend card must not present a measured FLOOR as the day's total.
 *
 * Measured defect (live, 2026-10-28/29): `GET /finance/summary.ai_spend` served
 * `spent: 0.0032` with `remaining`/`burn_rate` derived from it, on the same day whose
 * `day_spend_truth.total` was UNKNOWN because run 318 finished with no cost. The unmeasured agent
 * was named only inside `unit_economics`.
 *
 * The fixed wire (backend `AiSpendSummary`, wave 6): a finished run with no cost ⇒
 * `spent: null`, `spent_status: "partial"`, the measured portion SEPARATELY as
 * `measured_floor_usd`, and `unmeasured_finished_runs`. Pinned here:
 *
 *   * the value is the em dash, never the floor;
 *   * the note states the floor at its own precision and calls it a floor ("$0.0032", never
 *     "$0.00" — a floor rounded to a fabricated zero is the exact lie this project refuses);
 *   * the count of unpriced finished runs and the named agents are stated;
 *   * a pre-wave-6 server (no new fields) gets no invented state;
 *   * a measured day is unchanged, including its measured `$0.00`.
 */

type AiSpendBlock = NonNullable<FinanceSummary["ai_spend"]>;

const baseSummary: FinanceSummary = {
  mrr_cents: null,
  arr_cents: null,
  active_subscribers: null,
  total_ad_spend_usd: null,
  total_expenses_month_cents: null,
  stripe_balance_cents: null,
  last_snapshot_date: "2026-10-13",
};

/** The live mixed day, in miniature: one priced run ($0.0032) + one finished run with no cost. */
const partial: AiSpendBlock = {
  limit: 25.0,
  spent: null,
  remaining: null,
  spent_status: "partial",
  measured_floor_usd: 0.0032,
  unmeasured_finished_runs: 1,
  in_flight_runs: 0,
  burn_rate: null,
  unit_economics: {
    total_spend_usd: 0.0032,
    total_completed: 1,
    cost_per_completed_task_usd: 0.0032,
    per_agent: {
      social_media: { spend_usd: 0.0032, completed: 1, cost_per_completed_task_usd: 0.0032 },
    },
    note: "covers the priced agents only",
    unmeasured_spend_agents: ["email_outreach"],
  },
};

const unmeasuredAll: AiSpendBlock = {
  limit: 25.0,
  spent: null,
  remaining: null,
  spent_status: "unmeasured",
  measured_floor_usd: null,
  unmeasured_finished_runs: 1,
  in_flight_runs: 0,
  burn_rate: null,
  unit_economics: {
    total_spend_usd: 0.0,
    total_completed: 0,
    cost_per_completed_task_usd: null,
    per_agent: {},
    note: "no completions measured",
    unmeasured_spend_agents: ["email_outreach"],
  },
};

const measured: AiSpendBlock = {
  limit: 10.0,
  spent: 2.5,
  remaining: 7.5,
  spent_status: "measured",
  measured_floor_usd: null,
  unmeasured_finished_runs: 0,
  in_flight_runs: 1,
  burn_rate: null,
  unit_economics: {
    total_spend_usd: 2.5,
    total_completed: 1,
    cost_per_completed_task_usd: 2.5,
    per_agent: { finance: { spend_usd: 2.5, completed: 1, cost_per_completed_task_usd: 2.5 } },
    note: "covers the priced agents only",
    unmeasured_spend_agents: [],
  },
};

/** A pre-wave-6 server: the four new fields are absent, and nothing may be invented. */
const legacyUnmeasured: AiSpendBlock = {
  limit: 25.0,
  spent: null,
  remaining: null,
  burn_rate: null,
  unit_economics: null,
};

function aiSpendCard(): HTMLElement {
  const card = screen.getByText("AI spend today", { exact: true }).closest('[data-kpi-key="ai-spend"]');
  if (!card) throw new Error("the AI spend card must be rendered");
  return card as HTMLElement;
}

describe("F-W5-TS-1 — aiSpendValue/aiSpendNote (pure)", () => {
  it("a partial day's VALUE is the em dash — the floor is never promoted into the number", () => {
    expect(aiSpendValue(partial)).toBe("—");
    expect(aiSpendValue(partial)).not.toContain("$");
  });

  it("the note states the measured floor as a floor, at 4dp, and counts the unpriced run", () => {
    const note = aiSpendNote(partial) ?? "";
    expect(note).toContain("partial: at least $0.0032 measured so far — a floor, not the day's total");
    expect(note).toContain("1 finished run carried no cost");
    expect(note).toContain("unmeasured spend: email_outreach");
    // The fabricated-zero trap: $0.0032 must NOT be rounded down to exactly $0.00 (the word
    // boundary keeps "$0.0032" from matching, while a real "$0.00" reading would).
    expect(note).not.toMatch(/\$0\.00\b/);
  });

  it("an all-unmeasured day says the total is unknown and why", () => {
    const note = aiSpendNote(unmeasuredAll) ?? "";
    expect(note).toContain("unmeasured: 1 finished run carried no cost — the day's total is unknown");
    expect(note).toContain("unmeasured spend: email_outreach");
  });

  it("a partial day with no declared floor still refuses to guess one", () => {
    const note = aiSpendNote({ ...partial, measured_floor_usd: null }) ?? "";
    expect(note).toContain("partial: only part of the day's spend was measured");
    expect(note).not.toContain("at least");
  });

  it("a measured day keeps its measured value and gains no unknown-day note", () => {
    expect(aiSpendValue(measured)).toBe("$2.50 of $10.00");
    expect(aiSpendNote(measured)).toBeUndefined();
  });

  it("a pre-wave-6 server declares no state, so no state is invented", () => {
    expect(aiSpendValue(legacyUnmeasured)).toBe("—");
    expect(aiSpendNote(legacyUnmeasured)).toBeUndefined();
  });
});

describe("F-W5-TS-1 — the finance page's AI spend card", () => {
  afterEach(() => mockApiGet.mockReset());

  it("renders the partial day as the em dash plus the floor/floor-count note", async () => {
    mockApiGet.mockResolvedValue({ ...baseSummary, ai_spend: partial });

    render(<FinancePage />);

    await screen.findByText("AI spend today");
    expect(aiSpendCard()).toHaveTextContent("—");
    expect(aiSpendCard()).toHaveTextContent("a floor, not the day's total");
    expect(aiSpendCard()).toHaveTextContent("at least $0.0032");
    // The one number the card must NOT print for this day: a floor disguised as the total.
    expect(aiSpendCard()).not.toHaveTextContent("$0.0032 of $25.00");
    expect(aiSpendCard()).not.toHaveTextContent("$0.00 of $25.00");
  });
});

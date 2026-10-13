import { render, screen } from "@testing-library/react";

jest.mock("@/lib/api", () => ({ api: { get: jest.fn() } }));

import { api, type FinanceSummary } from "@/lib/api";
import FinancePage from "@/app/finance/page";

const mockApiGet = api.get as jest.MockedFunction<typeof api.get>;

/** Every money field null = "not measured" (the API's own no-snapshot state). */
const baseSummary: FinanceSummary = {
  mrr_cents: null,
  arr_cents: null,
  active_subscribers: null,
  total_ad_spend_usd: null,
  total_expenses_month_cents: null,
  stripe_balance_cents: null,
  last_snapshot_date: null,
};

/**
 * The real wire body for `ai_spend`, per build/R24-spend/raw/probe_ai_spend_wire.out — typed
 * against the ADOPTED `lib/api.ts` contract (`FinanceSummary["ai_spend"]`): the page no longer
 * declares its own copy (fix-4 §2 / T15a), so this fixture is the compile-time pin that the shared
 * type carries every field the card reads.
 */
const aiSpendMeasured: NonNullable<FinanceSummary["ai_spend"]> = {
  limit: 10.0,
  spent: 2.5,
  remaining: 7.5,
  burn_rate: {
    spent_usd: 2.5,
    day_limit_usd: 10.0,
    elapsed_fraction: 0.5668,
    projected_day_total_usd: 4.410941,
    status: "ok",
  },
  unit_economics: {
    total_spend_usd: 2.5,
    total_completed: 1,
    cost_per_completed_task_usd: 2.5,
    per_agent: { finance: { spend_usd: 2.5, completed: 1, cost_per_completed_task_usd: 2.5 } },
    note: "None means no completions measured; it must render as 'unmeasured', never as $0.00",
    unmeasured_spend_agents: ["social_media"],
  },
};

/** An unmeasured day: rows exist but no cost was reported — `spent`/`remaining` are null and the
 *  affected agent is NAMED in `unmeasured_spend_agents` (never priced at a fabricated $0.00). */
const aiSpendUnmeasured: NonNullable<FinanceSummary["ai_spend"]> = {
  limit: 10.0,
  spent: null,
  remaining: null,
  burn_rate: null,
  unit_economics: {
    total_spend_usd: 0.0,
    total_completed: 0,
    cost_per_completed_task_usd: null,
    per_agent: {},
    note: "None means no completions measured; it must render as 'unmeasured', never as $0.00",
    unmeasured_spend_agents: ["finance"],
  },
};

/** A measured ZERO day: `$0.00 of $10.00` — 0 and "not measured" stay apart on the page. */
const aiSpendZero: NonNullable<FinanceSummary["ai_spend"]> = {
  limit: 10.0,
  spent: 0.0,
  remaining: 10.0,
  burn_rate: null,
  unit_economics: null,
};

function aiSpendCard(): HTMLElement {
  const card = screen.getByText("AI spend today", { exact: true }).closest("div.bg-gray-800");
  if (!card) throw new Error("the AI spend card must be rendered");
  return card as HTMLElement;
}

describe("FinancePage — R24's AI spend card", () => {
  afterEach(() => mockApiGet.mockReset());

  it("renders a measured day as '$spent of $limit', with unmeasured agents named", async () => {
    mockApiGet.mockResolvedValue({ ...baseSummary, ai_spend: aiSpendMeasured });

    render(<FinancePage />);

    await screen.findByText("AI spend today");
    // Wait for the fetch to land (the card starts as an em dash).
    await screen.findByText("$2.50 of $10.00");
    expect(aiSpendCard()).toHaveTextContent("$2.50 of $10.00");
    expect(aiSpendCard()).toHaveTextContent("unmeasured spend: social_media");
  });

  it("an absent block renders the em dash — never $0.00", async () => {
    mockApiGet.mockResolvedValue({ ...baseSummary, ai_spend: null });

    render(<FinancePage />);

    await screen.findByText("AI spend today");
    expect(aiSpendCard()).toHaveTextContent("—");
    expect(aiSpendCard()).not.toHaveTextContent("$0.00");
  });

  it("an UNMEASURED day (spent: null) renders the em dash plus the note — and does not throw", async () => {
    mockApiGet.mockResolvedValue({ ...baseSummary, ai_spend: aiSpendUnmeasured });

    render(<FinancePage />);

    await screen.findByText("AI spend today");
    expect(aiSpendCard()).toHaveTextContent("—");
    expect(aiSpendCard()).toHaveTextContent("unmeasured spend: finance");
    expect(aiSpendCard()).not.toHaveTextContent("$0.00");
    // The page itself never failed — the unmeasured day is a measured state, not an error.
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("a measured ZERO renders $0.00 (the null law keeps 0 and 'not measured' apart)", async () => {
    mockApiGet.mockResolvedValue({ ...baseSummary, ai_spend: aiSpendZero });

    render(<FinancePage />);

    await screen.findByText("AI spend today");
    await screen.findByText("$0.00 of $10.00");
    expect(aiSpendCard()).toHaveTextContent("$0.00 of $10.00");
  });
});

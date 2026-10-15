/**
 * Wave V9 / seat b5-frontend — the planner page, the candidate card, the first-dollar checklist,
 * the finance Planning panel, the daily report's cash/tax block, the Business panel's gross-margin
 * row and the onboarding "Plan my company" link.
 *
 * The API module is mocked the way the sibling suites do it (u1/u2/u3/u7/u8b): every surface is
 * pinned on its BEHAVIOUR — what it reads, what it sends, and what it refuses to invent.
 *
 * Wire truth written from the wave-V9 plan's frozen contracts (§9 B1 FROZEN CONTRACT + B2's
 * planning shape), because the endpoints land in the same wave from sibling seats:
 *   POST /api/v1/planner/propose  {}         -> 201 {candidates: [Candidate], claims_unverified}
 *   POST /api/v1/planner/confirm  {candidate_id} -> 200 {confirmed: true}
 *   GET  /api/v1/planner/status              -> {confirmed: {...}|null, last_proposed_at: str|null}
 *   GET  /api/v1/finance/planning            -> {burn_line_items, capacity, margin_template,
 *                                                pricing_guardrails, ...} (anything unmeasured = null + reason)
 *   GET  /api/v1/dashboard/reports/daily     -> {...report, cash_block} (missing inputs null + reason, NEVER 0)
 *   PUT  /api/v1/config                      -> {checklist_state: {...}} (additive to the existing PUT)
 *
 * The live stack may not serve the four endpoints yet — this lane's contract is that a failed read
 * is NAMED on screen and never drawn as an empty list, a dash grid or a fabricated zero.
 */
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";

jest.mock("@/lib/api", () => {
  const actual = jest.requireActual("@/lib/api");
  return {
    ...actual,
    api: { get: jest.fn(), post: jest.fn(), put: jest.fn(), delete: jest.fn() },
  };
});

jest.mock("next/link", () => ({
  __esModule: true,
  default: function MockLink({
    href,
    children,
    className,
  }: {
    href: string;
    children: React.ReactNode;
    className?: string;
  }) {
    return (
      <a href={href} className={className}>
        {children}
      </a>
    );
  },
}));

jest.mock("next/navigation", () => ({
  usePathname: () => "/planner",
}));

import { api, ApiError } from "@/lib/api";
import PlannerPage from "@/app/planner/page";
import FinancePage from "@/app/finance/page";
import { BusinessPanel } from "@/components/dashboard/BusinessPanel";
import { DailyReportPanel } from "@/components/dashboard/DailyReportPanel";
import { DeepSeekKeyOnboarding } from "@/components/dashboard/DeepSeekKeyOnboarding";
import { FirstDollarChecklist } from "@/components/planner/FirstDollarChecklist";

const mockGet = api.get as jest.Mock;
const mockPost = api.post as jest.Mock;
const mockPut = api.put as jest.Mock;

beforeEach(() => {
  mockGet.mockReset();
  mockPost.mockReset();
  mockPut.mockReset();
  window.localStorage.clear();
});

/** Answer reads per path; an unlisted path fails LOUDLY so a new read cannot pass silently. */
function answerWith(overrides: Record<string, unknown> = {}) {
  const paths: Record<string, unknown> = {
    "/planner/status": { confirmed: null, last_proposed_at: null },
    // The REAL route shape (wave v14 S3-05): the three segments are ALWAYS objects on the wire
    // (nulls inside when unmeasured) and every unmeasured input carries its cause in `reasons`,
    // keyed 1:1 to the field — the field-level contract the Planning panel renders.
    "/finance/planning": {
      burn_line_items: [],
      capacity: {
        hours_per_client_lo: null,
        hours_per_client_hi: null,
        max_clients: null,
        mrr_at_capacity: null,
        break_even_clients: null,
        hours_per_client_source: null,
      },
      margin_template: {
        price: null,
        ai_cost: null,
        human_hours: null,
        human_rate: null,
        margin: null,
      },
      reasons: {},
      pricing_guardrails: [],
    },
    // The finance page's own reads. The Planning panel is mounted by the PAGE (it is not a
    // separate export), so rendering FinancePage means serving the whole page's contract.
    "/finance/summary": {
      mrr_cents: null,
      arr_cents: null,
      active_subscribers: null,
      total_ad_spend_usd: null,
      total_expenses_month_cents: null,
      stripe_balance_cents: null,
      last_snapshot_date: "2026-10-14",
    },
    "/finance/revenue": [],
    "/finance/expenses": [],
    "/finance/events": [],
    // Parent scope amendment: the market-context strip. The neutral default is the unmeasured
    // shape, so a planning test can never be fooled by an invented macro value.
    "/economics/context": {
      inflation: null,
      inflation_reason: "the last inflation fetch failed",
      fx: null,
      fx_reason: "no FX fetch has run yet",
      generated_at: null,
    },
    "/config": { checklist_state: {} },
    ...overrides,
  };
  mockGet.mockImplementation((path: string) => {
    // Longest prefix first: "/config/deepseek-key" must win over "/config" (the sibling suites'
    // "listed AFTER the deeper key" rule, done structurally so it cannot be got wrong by order).
    const key = Object.keys(paths)
      .sort((a, b) => b.length - a.length)
      .find((candidate) => path.startsWith(candidate));
    if (key === undefined) return Promise.reject(new Error(`unexpected read: ${path}`));
    const value = paths[key];
    return value instanceof Error ? Promise.reject(value) : Promise.resolve(value);
  });
}

/** One candidate in the frozen propose shape — a CM-POSITIVE one by default. */
function candidate(over: Record<string, unknown> = {}) {
  return {
    id: "c1",
    name: "Answering service for solo contractors",
    one_liner: "An agent answers missed calls for contractors and books the estimate.",
    buyer: "One-person home-service contractors",
    price_band: [300, 900],
    channel: "Direct email to a named list of 200 contractors",
    unit_cost_sketch: "$18/mo phone number + model tokens per call",
    rubric: {
      demand: 5,
      margin: 4,
      competition: 3,
      agent_fit: 4,
      time_to_first_dollar: 2,
      owner_fit: 5,
      total: 23,
    },
    gates: { cm_positive: true, budget_fit: null, hours_fit: null },
    claims: [
      {
        claim: "Contractors lose jobs to missed calls.",
        source_url: "https://example.test/research/missed-calls",
        fetched_at: "2026-10-14T10:00:00Z",
        quote: "42% of callers who reach voicemail never call back.",
        verified: true,
      },
    ],
    ...over,
  };
}

const PROPOSAL = { candidates: [candidate()], claims_unverified: 0 };

// ---------------------------------------------------------------------------------------------
// A — the planner page
// ---------------------------------------------------------------------------------------------

describe("PlannerPage — the empty state teaches the flow (plan §9 B5)", () => {
  it("renders three flow lines: propose, review gates/claims, confirm — and no marketing voice", async () => {
    answerWith();
    render(<PlannerPage />);

    await waitFor(() => expect(screen.getByTestId("planner-flow")).toBeInTheDocument());

    const steps = within(screen.getByTestId("planner-flow")).getAllByTestId("planner-flow-step");
    expect(steps).toHaveLength(3);
    expect(steps[0]).toHaveTextContent(/Propose/);
    expect(steps[1]).toHaveTextContent(/Review/);
    expect(steps[1]).toHaveTextContent(/gates/);
    expect(steps[1]).toHaveTextContent(/claims/);
    expect(steps[2]).toHaveTextContent(/Confirm/);
    // Confirming is the whole point: the state says what confirm does and what it does not do.
    expect(steps[2]).toHaveTextContent(/Nothing starts running/);
  });
});

describe("PlannerPage — propose", () => {
  it("POSTs /planner/propose and renders every candidate with rubric, gates, claims and CONFIRM", async () => {
    answerWith();
    mockPost.mockResolvedValue(PROPOSAL);

    render(<PlannerPage />);
    fireEvent.click(await screen.findByTestId("planner-propose"));

    const card = await screen.findByTestId("candidate-c1");
    expect(mockPost).toHaveBeenCalledWith("/planner/propose", {});

    // Rubric: per-axis numbers (each 0-5) and the total (0-30).
    expect(within(card).getByTestId("rubric-demand")).toHaveTextContent("5/5");
    expect(within(card).getByTestId("rubric-owner_fit")).toHaveTextContent("5/5");
    expect(within(card).getByTestId("rubric-total")).toHaveTextContent("23/30");

    // Gates: a PASS is stated too — a gate that is only visible when it fails is half the picture.
    expect(within(card).getByTestId("gate-cm")).toHaveTextContent("CM > 0 — gate passed");
    // The plan's own null rule: an unmeasured gate says so, it is never rendered as false.
    expect(within(card).getByTestId("gate-budget")).toHaveTextContent("not measured");

    // Claims list WITH its source link.
    const claims = within(card).getByTestId("claims");
    const link = within(claims).getByRole("link");
    expect(link).toHaveAttribute("href", "https://example.test/research/missed-calls");
    expect(claims).toHaveTextContent(/42% of callers who reach voicemail never call back/);

    expect(within(card).getByRole("button", { name: /Confirm this idea/ })).toBeInTheDocument();
  });

  it("shows a CM ≤ 0 candidate as a flag chip — not hidden, not a red error", async () => {
    answerWith();
    mockPost.mockResolvedValue({
      candidates: [
        candidate({
          id: "c2",
          name: "Subsidised newsletter",
          gates: { cm_positive: false, budget_fit: true, hours_fit: null },
        }),
      ],
      claims_unverified: 3,
    });

    render(<PlannerPage />);
    fireEvent.click(await screen.findByTestId("planner-propose"));

    const card = await screen.findByTestId("candidate-c2");
    const gate = within(card).getByTestId("gate-cm");
    expect(gate).toHaveTextContent("CM ≤ 0 — blocked");
    expect(gate).toHaveClass("chip");
    expect(gate).not.toHaveClass("chip-alert");
    expect(within(card).queryByRole("alert")).toBeNull();
    // The candidate is never silently dropped (B1's rule), and the stripped-claim count is stated.
    expect(within(card).getByRole("button", { name: /Confirm this idea/ })).toBeInTheDocument();
    expect(screen.getByTestId("planner-claims-unverified")).toHaveTextContent(
      "3 claims were dropped — the model stated them without a fetched source.",
    );
  });

  it("names a failed propose instead of drawing an empty candidate list", async () => {
    answerWith();
    mockPost.mockRejectedValue(new ApiError(503, "planner unavailable", "E_HTTP_503", "retry later"));

    render(<PlannerPage />);
    fireEvent.click(await screen.findByTestId("planner-propose"));

    const alert = await screen.findByTestId("planner-propose-error");
    expect(alert).toHaveTextContent("E_HTTP_503: planner unavailable — retry later");
    expect(screen.queryByTestId("planner-candidates")).toBeNull();
    // No dead button: the proposal can be asked for again.
    expect(screen.getByTestId("planner-propose")).toBeEnabled();
  });
});

describe("PlannerPage — confirm", () => {
  it("confirms by id, states what was saved, and that nothing runs from it", async () => {
    answerWith();
    mockPost.mockResolvedValueOnce(PROPOSAL).mockResolvedValueOnce({ confirmed: true });
    // The page re-reads the status after a confirm: the saved idea is the server's answer, not ours.
    mockGet.mockImplementation((path: string) => {
      if (path.startsWith("/planner/status")) {
        return Promise.resolve(
          mockPost.mock.calls.length > 0
            ? {
                confirmed: {
                  candidate_id: "c1",
                  name: "Answering service for solo contractors",
                  one_liner: "An agent answers missed calls for contractors.",
                  confirmed_at: "2026-10-14T16:00:00Z",
                },
                last_proposed_at: "2026-10-14T15:59:00Z",
              }
            : { confirmed: null, last_proposed_at: null },
        );
      }
      if (path.startsWith("/config")) return Promise.resolve({ checklist_state: {} });
      return Promise.resolve({ burn_line_items: [], capacity: null, margin_template: null });
    });

    render(<PlannerPage />);
    fireEvent.click(await screen.findByTestId("planner-propose"));
    fireEvent.click(await screen.findByRole("button", { name: /Confirm this idea/ }));

    await waitFor(() =>
      expect(mockPost).toHaveBeenCalledWith("/planner/confirm", { candidate_id: "c1" }),
    );

    const note = await screen.findByTestId("planner-confirmed-note");
    expect(note).toHaveTextContent("Answering service for solo contractors");
    expect(note).toHaveTextContent(/Nothing was scheduled/);
    expect(note).toHaveTextContent(/no task was created/);

    // NOTHING ELSE RUNS: the only writes this page ever makes are propose + confirm.
    const paths = mockPost.mock.calls.map(([path]) => path);
    expect(paths).toEqual(["/planner/propose", "/planner/confirm"]);
    expect(mockPut).not.toHaveBeenCalled();
    expect(api.delete).not.toHaveBeenCalled();

    // The saved idea is then read back from the status endpoint.
    const confirmed = await screen.findByTestId("planner-confirmed");
    expect(confirmed).toHaveTextContent("Answering service for solo contractors");
  });

  it("never claims a save that failed — the refusal is named", async () => {
    answerWith();
    mockPost
      .mockResolvedValueOnce(PROPOSAL)
      .mockRejectedValueOnce(new ApiError(409, "no proposal to confirm", "E_CONFLICT", "propose again"));

    render(<PlannerPage />);
    fireEvent.click(await screen.findByTestId("planner-propose"));
    fireEvent.click(await screen.findByRole("button", { name: /Confirm this idea/ }));

    const error = await screen.findByTestId("planner-confirm-error");
    expect(error).toHaveTextContent("E_CONFLICT: no proposal to confirm — propose again");
    expect(screen.queryByTestId("planner-confirmed-note")).toBeNull();
  });
});

describe("PlannerPage — status", () => {
  it("states the confirmed idea the status read answered", async () => {
    answerWith({
      "/planner/status": {
        confirmed: {
          candidate_id: "c9",
          name: "Local SEO reports for dentists",
          one_liner: "Monthly report product.",
          confirmed_at: "2026-10-13T09:00:00Z",
        },
        last_proposed_at: "2026-10-13T08:00:00Z",
      },
    });

    render(<PlannerPage />);

    const confirmed = await screen.findByTestId("planner-confirmed");
    expect(confirmed).toHaveTextContent("Local SEO reports for dentists");
    expect(screen.queryByTestId("planner-flow")).toBeNull();
  });

  it("names a failed status read instead of rendering 'nothing confirmed'", async () => {
    answerWith({ "/planner/status": new ApiError(500, "boom", "E_HTTP_500", "check the backend") });

    render(<PlannerPage />);

    const alert = await screen.findByTestId("planner-status-error");
    expect(alert).toHaveTextContent("E_HTTP_500: boom — check the backend");
    expect(screen.queryByTestId("planner-flow")).toBeNull();
  });
});

// ---------------------------------------------------------------------------------------------
// B — the first-dollar checklist
// ---------------------------------------------------------------------------------------------

describe("FirstDollarChecklist (plan §9 B5 + v1.1 renewal clocks)", () => {
  it("carries a source link and a real cost on every step, including the three renewal clocks", async () => {
    answerWith();
    render(<FirstDollarChecklist />);

    const checklist = await screen.findByTestId("first-dollar-checklist");

    // Each step states its real cost and links the page the cost came from.
    const ein = within(checklist).getByTestId("checklist-step-ein");
    expect(ein).toHaveTextContent("$0");
    expect(within(ein).getByRole("link")).toHaveAttribute(
      "href",
      "https://www.irs.gov/businesses/small-businesses-self-employed/get-an-employer-identification-number",
    );

    // The renewal-clock items, each with ITS source line (PLAYBOOK §1.3 / §3.1 / §1.2).
    const renewal = within(checklist).getByTestId("checklist-step-mn-llc-renewal");
    expect(renewal).toHaveTextContent("$0");
    expect(renewal).toHaveTextContent(/Annual Renewal \| \$0/);
    expect(within(renewal).getByRole("link")).toHaveAttribute(
      "href",
      "https://www.sos.mn.gov/business-liens/business-filing-and-certification-fee-schedule/",
    );

    const insurance = within(checklist).getByTestId("checklist-step-insurance-review");
    expect(within(insurance).getByRole("link")).toHaveAttribute(
      "href",
      "https://mn.gov/deed/dobusiness/doing-business-here/grow-your-business/five-steps.jsp",
    );
    // Not priced by the playbook → said so, never invented.
    expect(insurance).toHaveTextContent(/no fee is priced in the playbook/);

    const audit = within(checklist).getByTestId("checklist-step-subscription-audit");
    expect(audit).toHaveTextContent(/\$0/);
    expect(audit).toHaveTextContent(/§1.2/);
    expect(within(audit).getByRole("link")).toBeInTheDocument();
  });

  it("persists a ticked step through PUT /config checklist_state", async () => {
    answerWith();
    mockPut.mockResolvedValue({ checklist_state: {} });

    render(<FirstDollarChecklist />);
    const checkbox = await screen.findByRole("checkbox", { name: /Get an EIN/ });
    expect(checkbox).not.toBeChecked();

    fireEvent.click(checkbox);

    await waitFor(() => expect(mockPut).toHaveBeenCalledTimes(1));
    const [path, body] = mockPut.mock.calls[0];
    expect(path).toBe("/config");
    expect(body.checklist_state.ein.done).toBe(true);
    expect(typeof body.checklist_state.ein.done_at).toBe("string");
    // Additive to the existing config PUT: nothing else is overwritten.
    expect(Object.keys(body)).toEqual(["checklist_state"]);
  });

  it("keeps the stored ticks on a re-read and names a failed save (never a false tick)", async () => {
    answerWith({
      "/config": { checklist_state: { ein: { done: true, done_at: "2026-10-13T09:00:00Z" } } },
    });
    mockPut.mockRejectedValue(new ApiError(422, "E_VALIDATION", "E_VALIDATION", "check the field"));

    render(<FirstDollarChecklist />);

    const ein = await screen.findByRole("checkbox", { name: /Get an EIN/ });
    expect(ein).toBeChecked();

    const bank = screen.getByRole("checkbox", { name: /Open a business bank account/ });
    fireEvent.click(bank);

    const error = await screen.findByTestId("checklist-save-error");
    expect(error).toHaveTextContent("E_VALIDATION");
    // The box must not stay ticked: a state the server never stored is a lie.
    await waitFor(() => expect(bank).not.toBeChecked());
    expect(ein).toBeChecked(); // the already-stored tick is untouched
  });

  it("renders a named failure and NO live checkboxes when the config read fails", async () => {
    answerWith({ "/config": new ApiError(500, "config down", "E_HTTP_500", "restart the backend") });

    render(<FirstDollarChecklist />);

    const alert = await screen.findByTestId("checklist-read-error");
    expect(alert).toHaveTextContent("E_HTTP_500: config down — restart the backend");
    expect(screen.queryByRole("checkbox")).toBeNull();
  });
});

// ---------------------------------------------------------------------------------------------
// C — the finance Planning panel
// ---------------------------------------------------------------------------------------------

/** The REAL route shape of GET /finance/planning (`build_planning_view`, wave v14 S3-05): the three
 *  segments are ALWAYS objects on the wire (nulls inside when unmeasured) and every unmeasured input
 *  carries its cause in the `reasons` map, keyed 1:1 to the field it explains. A fixture built
 *  through this helper cannot drift from the route's shape the way the old hand-built one did. */
function planningBody(over: Record<string, unknown> = {}) {
  return {
    burn_line_items: [],
    capacity: {
      hours_per_client_lo: null,
      hours_per_client_hi: null,
      max_clients: null,
      mrr_at_capacity: null,
      break_even_clients: null,
      hours_per_client_source: null,
    },
    margin_template: {
      price: null,
      ai_cost: null,
      human_hours: null,
      human_rate: null,
      margin: null,
    },
    reasons: {},
    pricing_guardrails: [],
    sales_tax_note: "Sales tax is collected where economic nexus applies.",
    tax_disclaimer: "Not tax advice — verify with MN Revenue.",
    catalog_as_of: "2026-10-14",
    staleness_days: 1,
    stale: false,
    ...over,
  };
}

describe("FinancePage — the Planning panel (GET /finance/planning)", () => {
  it("renders burn line items, capacity and the margin template", async () => {
    answerWith({
      "/finance/planning": planningBody({
        burn_line_items: [
          { label: "Backend host", monthly_usd: 12, source: "config:host_plan" },
          { label: "Model tokens", monthly_usd: 30, source: "runs ledger, 30 days" },
        ],
        capacity: {
          hours_per_client_lo: 2,
          hours_per_client_hi: 4,
          max_clients: 12,
          mrr_at_capacity: 3600,
          break_even_clients: 2,
          hours_per_client_source: "SEO — small business",
        },
        margin_template: {
          price: 500,
          ai_cost: 20,
          human_hours: 3,
          human_rate: 40,
          margin: 0.38,
        },
      }),
    });

    render(<FinancePage />);

    const panel = await screen.findByTestId("finance-planning");
    expect(within(panel).getByText("Backend host")).toBeInTheDocument();
    expect(within(panel).getByText("$12.00")).toBeInTheDocument();
    expect(within(panel).getByTestId("planning-capacity")).toHaveTextContent("12");
    expect(within(panel).getByTestId("planning-margin")).toHaveTextContent("$500.00");
    expect(within(panel).getByTestId("planning-margin")).toHaveTextContent("38.0%");
    // A measured input carries no reason — the panel prints only what the payload records.
    expect(within(panel).queryAllByTestId("planning-reason")).toHaveLength(0);
  });

  it("renders every unmeasured input as the em dash PLUS its cause from the payload's `reasons` map — never as $0.00 (S3-05)", async () => {
    answerWith({
      "/finance/planning": planningBody({
        burn_line_items: [
          { label: "Model tokens", monthly_usd: null, source: null, reason: "no runs recorded yet" },
        ],
        reasons: {
          "burn_line_items": "no expense records for 2026-10 — monthly burn is unmeasured",
          "capacity.hours_per_client_lo":
            "no catalog row matches the confirmed idea, so hours per client are unmeasured",
          "capacity.hours_per_client_hi":
            "no catalog row matches the confirmed idea, so hours per client are unmeasured",
          "capacity.max_clients": "owner_hours_per_week is not set in the company profile",
          "margin_template.price":
            "no confirmed idea with a numeric price band is configured (run the planner and confirm a candidate)",
          "margin_template.ai_cost": "no AI cost measured in the last 30 days (no runs reported a cost)",
          "margin_template.human_hours":
            "no catalog row matches the confirmed idea, so hours per client are unmeasured",
          "margin_template.human_rate":
            "no human hourly rate is configured anywhere (the owner profile carries hours, not a rate)",
          "margin_template.margin":
            "needs a measured price, ai_cost, human_hours and human_rate — see the fields above",
        },
      }),
    });

    render(<FinancePage />);

    const panel = await screen.findByTestId("finance-planning");
    expect(within(panel).getAllByText("—").length).toBeGreaterThan(0);
    // The null burn row's own cause …
    expect(within(panel).getByText(/no runs recorded yet/)).toBeInTheDocument();
    // … and each capacity/margin reading's cause, printed next to the reading it explains.
    const capacity = within(panel).getByTestId("planning-capacity");
    expect(capacity).toHaveTextContent("owner_hours_per_week is not set in the company profile");
    expect(capacity).toHaveTextContent("no catalog row matches the confirmed idea");
    const margin = within(panel).getByTestId("planning-margin");
    expect(margin).toHaveTextContent("no human hourly rate is configured anywhere");
    expect(margin).toHaveTextContent("no confirmed idea with a numeric price band");
    expect(within(panel).queryByText("$0.00")).toBeNull();
  });

  it("a segment answered as null says 'not measured' with the payload's reason — never a hardcoded cause", async () => {
    answerWith({
      "/finance/planning": planningBody({
        capacity: null,
        margin_template: null,
        burn_line_items: [],
        reasons: {
          "burn_line_items": "no expense records for 2026-10 — monthly burn is unmeasured",
          "capacity.max_clients": "owner_hours_per_week is not set in the company profile",
          "margin_template.margin":
            "needs a measured price, ai_cost, human_hours and human_rate — see the fields above",
        },
      }),
    });

    render(<FinancePage />);

    const panel = await screen.findByTestId("finance-planning");
    expect(within(panel).getByTestId("planning-burn-empty")).toHaveTextContent(
      /no expense records for 2026-10/,
    );
    expect(within(panel).getByTestId("planning-capacity-unmeasured")).toHaveTextContent(
      "owner_hours_per_week is not set in the company profile",
    );
    expect(within(panel).getByTestId("planning-margin-unmeasured")).toHaveTextContent(
      "needs a measured price",
    );
  });

  it("renders the pricing-model guardrails when the endpoint sends them", async () => {
    answerWith({
      "/finance/planning": planningBody({
        pricing_guardrails: [
          "Hourly pricing: the model caps what one operator can earn.",
          "Rev-share: the model cannot be automated end to end.",
        ],
      }),
    });

    render(<FinancePage />);

    const guardrails = await screen.findByTestId("planning-guardrails");
    expect(guardrails).toHaveTextContent(/Hourly pricing/);
    expect(guardrails).toHaveTextContent(/Rev-share/);
  });

  it("names a failed planning read", async () => {
    answerWith({
      "/finance/planning": new ApiError(404, "not found", "E_HTTP_404", "the endpoint lands this wave"),
    });

    render(<FinancePage />);

    const alert = await screen.findByText(/Could not load the finance planning view/);
    expect(alert).toHaveTextContent("E_HTTP_404");
  });
});

// ---------------------------------------------------------------------------------------------
// D — the daily report's cash/tax block
// ---------------------------------------------------------------------------------------------

describe("DailyReportPanel — cash/tax block (plan §9 B2)", () => {
  function reportWith(cashBlock: unknown) {
    return {
      id: 5,
      report_date: "2026-10-14",
      morning_plan: "Morning plan text",
      evening_summary: "Evening summary text",
      tasks_planned: 4,
      tasks_completed: 3,
      tasks_failed: 0,
      updated_at: "2026-10-14T12:00:00Z",
      cash_block: cashBlock,
    };
  }

  it("renders the block's fields and the persistent tax line", async () => {
    answerWith({
      "/dashboard/reports/daily": reportWith({
        cash_on_hand: 4200.5,
        in_30d: 1200,
        out_30d: 800,
        tax_set_aside: 600,
        next_tax_dates: ["2027-01-15", "2027-04-15", "2027-06-15", "2027-09-15"],
        safe_harbor_note: "Pay the smaller of 90% of this year or 100% of last year.",
      }),
    });

    render(<DailyReportPanel />);

    const block = await screen.findByTestId("cash-tax-block");
    expect(within(block).getByTestId("cash-on-hand")).toHaveTextContent("$4,200.50");
    expect(within(block).getByTestId("cash-in-30d")).toHaveTextContent("$1,200.00");
    expect(within(block).getByTestId("cash-out-30d")).toHaveTextContent("$800.00");
    expect(within(block).getByTestId("cash-tax-set-aside")).toHaveTextContent("$600.00");
    expect(within(block).getByTestId("cash-next-tax-dates")).toHaveTextContent("2027-04-15");
    expect(block).toHaveTextContent(/90% of this year/);
    expect(within(block).getByTestId("tax-disclaimer")).toHaveTextContent(
      "Not tax advice — verify with MN Revenue.",
    );
  });

  it("renders an honest line + reason when cash_block is null — never a zero, disclaimer still there", async () => {
    answerWith({ "/dashboard/reports/daily": reportWith(null) });

    render(<DailyReportPanel />);

    const block = await screen.findByTestId("cash-tax-block");
    expect(within(block).getByTestId("cash-block-unmeasured")).toHaveTextContent(
      /no cash inputs have been measured/,
    );
    expect(block).not.toHaveTextContent("$0.00");
    expect(within(block).getByTestId("tax-disclaimer")).toHaveTextContent(
      "Not tax advice — verify with MN Revenue.",
    );
  });

  it("keeps a null field null inside a present block, with the server's reason", async () => {
    answerWith({
      "/dashboard/reports/daily": reportWith({
        cash_on_hand: null,
        cash_reason: "no finance snapshot has been recorded yet",
        in_30d: 500,
        out_30d: null,
        tax_set_aside: null,
        next_tax_dates: [],
        safe_harbor_note: null,
      }),
    });

    render(<DailyReportPanel />);

    const block = await screen.findByTestId("cash-tax-block");
    expect(within(block).getByTestId("cash-on-hand")).toHaveTextContent("—");
    expect(within(block).getByTestId("cash-on-hand")).toHaveTextContent(
      "no finance snapshot has been recorded yet",
    );
    expect(within(block).getByTestId("cash-in-30d")).toHaveTextContent("$500.00");
    expect(block).not.toHaveTextContent("$0.00");
  });
});

// ---------------------------------------------------------------------------------------------
// E — the Business panel's gross-margin row
// ---------------------------------------------------------------------------------------------

describe("BusinessPanel — GROSS MARGIN row (plan §9 v1.1)", () => {
  it("renders the margin the planning read measured", async () => {
    answerWith({
      "/dashboard/summary": {
        tasks_today_total: 7,
        tasks_today_completed: 3,
        tasks_today_pending: 3,
        tasks_today_failed: 1,
        active_agents: ["finance"],
        kpis: { mrr_usd: 1234 },
        last_report_date: null,
      },
      "/finance/planning": {
        burn_line_items: [],
        capacity: null,
        margin_template: { price: 500, ai_cost: 20, human_hours: 3, human_rate: 40, margin: 0.38 },
        pricing_guardrails: [],
      },
    });

    render(<BusinessPanel />);

    expect(await screen.findByText("Gross margin")).toBeInTheDocument();
    expect(screen.getByTestId("gross-margin-value")).toHaveTextContent("38.0%");
    expect(screen.queryByTestId("margin-unmeasured")).toBeNull();
  });

  it("shows the em dash PLUS the reason while the planning inputs do not exist", async () => {
    answerWith({
      "/dashboard/summary": {
        tasks_today_total: 0,
        tasks_today_completed: 0,
        tasks_today_pending: 0,
        tasks_today_failed: 0,
        active_agents: [],
        kpis: {},
        last_report_date: null,
      },
      "/finance/planning": {
        burn_line_items: [],
        capacity: null,
        margin_template: {
          price: null,
          ai_cost: null,
          human_hours: null,
          human_rate: null,
          margin: null,
        },
        // The REAL route shape (v14 S3-05): the cause rides the `reasons` map, keyed to the field.
        reasons: { "margin_template.margin": "no price or cost inputs are configured yet" },
        pricing_guardrails: [],
      },
    });

    render(<BusinessPanel />);

    expect(await screen.findByTestId("gross-margin-value")).toHaveTextContent("—");
    expect(screen.getByTestId("margin-unmeasured")).toHaveTextContent(
      "no price or cost inputs are configured yet",
    );
  });

  it("names a failed planning read on the margin row — the other rows keep their values", async () => {
    answerWith({
      "/dashboard/summary": {
        tasks_today_total: 7,
        tasks_today_completed: 3,
        tasks_today_pending: 3,
        tasks_today_failed: 1,
        active_agents: ["finance"],
        kpis: { mrr_usd: 1234 },
        last_report_date: null,
      },
      "/finance/planning": new ApiError(503, "planning down", "E_HTTP_503", "retry"),
    });

    render(<BusinessPanel />);

    expect(await screen.findByTestId("gross-margin-value")).toHaveTextContent("—");
    expect(screen.getByTestId("margin-read-error")).toHaveTextContent("E_HTTP_503: planning down — retry");
    expect(screen.getByText("$1234")).toBeInTheDocument(); // the summary read is unaffected
  });
});

// ---------------------------------------------------------------------------------------------
// F — the onboarding "Plan my company" link
// ---------------------------------------------------------------------------------------------

describe("DeepSeekKeyOnboarding — the key-OK state", () => {
  it("offers the planner once the key is configured, without touching the setup banner", async () => {
    answerWith({
      "/config/deepseek-key": { configured: true, source: "stored", masked: "sk-…1234" },
    });

    render(<DeepSeekKeyOnboarding />);

    const link = await screen.findByRole("link", { name: "Plan my company" });
    expect(link).toHaveAttribute("href", "/planner");
    // The banner + the Settings link keep their behaviour: they are for the NOT-configured state.
    expect(screen.queryByTestId("deepseek-onboarding")).toBeNull();
    expect(screen.queryByRole("link", { name: /Open Settings/ })).toBeNull();
  });

  it("keeps the setup banner (and its Settings link) while no key is configured", async () => {
    answerWith({
      "/config/deepseek-key": { configured: false, source: "none", masked: "…" },
    });

    render(<DeepSeekKeyOnboarding />);

    const banner = await screen.findByTestId("deepseek-onboarding");
    expect(banner).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Open Settings/ })).toHaveAttribute("href", "/settings");
    expect(screen.queryByRole("link", { name: "Plan my company" })).toBeNull();
  });
});

// ---------------------------------------------------------------------------------------------
// G — the market context strip (parent scope amendment, owner-driven)
// ---------------------------------------------------------------------------------------------

describe("FinancePage — the market context strip (GET /economics/context)", () => {
  it("renders ONE compact line carrying each fact's source label and fetched-at stamp", async () => {
    answerWith({
      "/economics/context": {
        inflation: {
          value: 3.1,
          period: "Sep 2026",
          source: "U.S. BLS",
          fetched_at: "2026-10-14T19:30:00Z",
        },
        fx: {
          value: "1.08 USD/EUR",
          period: "2026-10-14",
          source: "ECB daily reference",
          fetched_at: "2026-10-14T18:00:00Z",
        },
        generated_at: "2026-10-14T20:00:00Z",
      },
    });

    render(<FinancePage />);

    const line = await screen.findByTestId("market-context");
    expect(line.tagName).toBe("P"); // ONE line, not a block grid
    const inflation = within(line).getByTestId("market-context-inflation");
    expect(inflation).toHaveTextContent("3.1");
    expect(inflation).toHaveTextContent("Sep 2026");
    expect(inflation).toHaveTextContent("U.S. BLS");
    expect(inflation).toHaveTextContent(/fetched/);
    const fx = within(line).getByTestId("market-context-fx");
    expect(fx).toHaveTextContent("1.08 USD/EUR");
    expect(fx).toHaveTextContent("ECB daily reference");
    expect(fx).toHaveTextContent(/fetched/);
    expect(screen.queryByTestId("market-context-error")).toBeNull();
  });

  it("states the reason for a null section — never an invented value, never a hidden null", async () => {
    answerWith({
      "/economics/context": {
        inflation: null,
        inflation_reason: "the last BLS fetch failed",
        fx: null,
        fx_reason: "no FX fetch has run yet",
        generated_at: null,
      },
    });

    render(<FinancePage />);

    const line = await screen.findByTestId("market-context");
    expect(within(line).getByTestId("market-context-inflation")).toHaveTextContent(
      "the last BLS fetch failed",
    );
    expect(within(line).getByTestId("market-context-fx")).toHaveTextContent(
      "no FX fetch has run yet",
    );
    expect(line).toHaveTextContent(/not measured/);
    expect(screen.queryByTestId("market-context-error")).toBeNull();
  });

  it("names a failed market read — the strip is never silently missing", async () => {
    answerWith({
      "/economics/context": new ApiError(503, "econ unavailable", "E_HTTP_503", "retry later"),
    });

    render(<FinancePage />);

    const alert = await screen.findByTestId("market-context-error");
    expect(alert).toHaveTextContent("E_HTTP_503: econ unavailable — retry later");
    expect(screen.queryByTestId("market-context")).toBeNull();
  });

  it("renders the LIVE wire shapes: a null-value section's reason + the fx rate map — measured facts are never lost (parent settle)", async () => {
    // The sibling economics seat shipped richer shapes than the first mock: inflation carries a
    // null value WITH its reason inside the object; fx carries date + a rates map (no `value`).
    // The strip must explain the dash and must not drop the rates that were actually measured.
    answerWith({
      "/economics/context": {
        inflation: {
          value: null,
          period: "2025",
          source: "https://api.worldbank.org/v2/country/USA/indicator/FP.CPI.TOTL.ZG",
          fetched_at: "2026-10-14T22:56:22+00:00",
          reason: "World Bank reports no value for 2025 — the newest year is not yet reported",
        },
        inflation_reason: null,
        fx: {
          usd_base: true,
          date: "2026-10-14",
          rates: { EUR: 0.89326, GBP: 0.75377 },
          source: "https://api.frankfurter.dev/v1/latest?base=USD",
          fetched_at: "2026-10-14T22:56:22+00:00",
          reason: null,
        },
        fx_reason: null,
        generated_at: "2026-10-14T22:57:51+00:00",
      },
    });

    render(<FinancePage />);

    const line = await screen.findByTestId("market-context");
    const inflation = within(line).getByTestId("market-context-inflation");
    expect(inflation).toHaveTextContent("the newest year is not yet reported");
    expect(inflation).toHaveTextContent("worldbank");
    const fx = within(line).getByTestId("market-context-fx");
    expect(fx).toHaveTextContent("EUR 0.89326");
    expect(fx).toHaveTextContent("2026-10-14");
    expect(fx).not.toHaveTextContent(/FX —/);
    expect(screen.queryByTestId("market-context-error")).toBeNull();
  });
});

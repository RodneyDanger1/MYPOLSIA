/**
 * WAVE 2 / F-01 — Settings must expose the four fields the agents read.
 *
 * The measured defect (Visuals F-01): 24 editable controls on `/settings` and NONE touched
 * `pricing_model`, `goals`, `github_repo` or `confirmed_idea`, while four agents quoted the stored
 * (seed) values back at a candle company.  These tests pin the fixed screen-to-row path:
 *
 *   textarea/input -> ONE single-key PUT /config -> the SERVER's answer is what the page shows.
 *
 * `confirmed_idea` stays read-only ON PURPOSE: the Planner owns that write, and the test asserts
 * there is no second editor for it (only a labelled view + a link to the Planner).
 *
 * The HTTP client is the permitted double (operating contract §7); the wire shapes are the
 * backend's own `CompanyConfigResponse`/`CompanyConfigUpdate`.
 */
import { fireEvent, render, screen, waitFor } from "@testing-library/react";

// The Settings page mounts many sections (form, KPI editor, this one); on a loaded machine a cold
// render can exceed Jest's 5 s default even though nothing is slow in this suite. The bound is
// raised, no assertion is relaxed (measured: one 5 s timeout under concurrent seat load, 9/9 green
// standalone immediately after).
jest.setTimeout(15_000);

jest.mock("@/lib/api", () => {
  const actual = jest.requireActual("@/lib/api");
  return {
    ...actual,
    api: { get: jest.fn(), post: jest.fn(), put: jest.fn(), delete: jest.fn() },
  };
});

import { ApiError, api } from "@/lib/api";
import SettingsPage, { type SettingsConfig } from "@/app/settings/page";

const mockGet = api.get as jest.Mock;
const mockPut = api.put as jest.Mock;

const PRICING = {
  currency: "USD",
  model: "physical_dtc",
  skus: [
    { sku: "CP-FIRE-8", name: "Fireside", retail: 28, unit_cost: 9.4 },
    { sku: "CP-CEDAR-8", name: "Cedar & Sage", retail: 28, unit_cost: 9.4 },
  ],
};

const GOALS = {
  launch_60d: ["Sell 300 units in the first 90 days", "Get into 5 boutiques"],
  kpi_targets: { cac_max_usd: 22, ltv_min_usd: 96 },
};

const IDEA = {
  id: "idea-4fbfbdc0154d",
  name: "Hand-poured coconut-soy candle line",
  one_liner: "A $28 wood-wick candle with a 55-hour burn.",
  buyer: "US women 28-45 who buy home decor",
  price_band: [24, 38],
};

const CONFIG: SettingsConfig = {
  name: "Copper & Pine Candle Co.",
  mission: null,
  vision: null,
  description: null,
  target_market: null,
  value_prop: null,
  website_url: null,
  industry: "Home fragrance",
  product_type: "Physical product",
  timezone: "UTC",
  daily_cycle_hour: 6,
  pricing_model: PRICING,
  goals: GOALS,
  kpis: {},
  github_repo: "yourorg/yourrepo",
  confirmed_idea: IDEA,
};

function serveReads(config: SettingsConfig = CONFIG) {
  mockGet.mockImplementation((path: string) => {
    if (path === "/config") return Promise.resolve(config);
    if (path === "/config/deepseek-key") {
      return Promise.resolve({ configured: false, source: "none", masked: "" });
    }
    return Promise.reject(new Error(`unexpected GET ${path}`));
  });
}

async function renderSettings(): Promise<void> {
  serveReads();
  render(<SettingsPage />);
  await screen.findByTestId("company-plan-section");
}

const pricingBox = () =>
  screen.getByLabelText("Pricing Model") as HTMLTextAreaElement;
const goalsBox = () => screen.getByLabelText("Goals") as HTMLTextAreaElement;
const repoBox = () =>
  screen.getByLabelText("GitHub Repo (owner/repo)") as HTMLInputElement;

beforeEach(() => {
  mockGet.mockReset();
  mockPut.mockReset();
});

describe("F-01 — the agent-context fields are reachable on the screen", () => {
  it("renders Pricing Model, Goals, GitHub Repo and a READ-ONLY Confirmed Idea", async () => {
    await renderSettings();

    expect(pricingBox()).toBeInTheDocument();
    expect(goalsBox()).toBeInTheDocument();
    expect(repoBox()).toBeInTheDocument();

    // confirmed_idea: shown, labelled read-only, and NOT an editable control.
    const idea = screen.getByTestId("settings-confirmed-idea");
    expect(idea).toHaveTextContent("Confirmed Idea (read-only — written by the Planner)");
    expect(idea).toHaveTextContent(IDEA.name);
    expect(idea).toHaveTextContent("$24–$38");
    expect(idea.querySelector("textarea")).toBeNull();
    expect(idea.querySelector("input")).toBeNull();
    const planner = screen.getByRole("link", { name: "Open the Planner" });
    expect(planner).toHaveAttribute("href", "/planner");

    // Opening Settings fired ONE read: the page's own /config. The section adds none.
    expect(mockGet.mock.calls.filter(([p]) => p === "/config")).toHaveLength(1);
    expect(mockGet).toHaveBeenCalledTimes(1);
  });

  it("seeds every box from the server row — exact JSON, no invented values", async () => {
    await renderSettings();

    expect(JSON.parse(pricingBox().value)).toEqual(PRICING);
    expect(JSON.parse(goalsBox().value)).toEqual(GOALS);
    expect(repoBox().value).toBe("yourorg/yourrepo");

    // The stored-value line is a human summary of what the SERVER served.
    expect(screen.getByTestId("settings-pricing-model-stored")).toHaveTextContent(
      "Fireside — $28 · Cedar & Sage — $28",
    );
    expect(screen.getByTestId("settings-goals-stored")).toHaveTextContent("2 groups");
    expect(screen.getByTestId("settings-github-repo-stored")).toHaveTextContent(
      "Stored now: yourorg/yourrepo",
    );
  });

  it("a null row renders EMPTY boxes and says no value — never a fabricated {}", async () => {
    serveReads({ ...CONFIG, pricing_model: null, goals: null, github_repo: null, confirmed_idea: null });
    render(<SettingsPage />);
    await screen.findByTestId("company-plan-section");

    expect(pricingBox().value).toBe("");
    expect(goalsBox().value).toBe("");
    expect(repoBox().value).toBe("");
    expect(screen.getByTestId("settings-pricing-model-stored")).toHaveTextContent("no value");
    expect(screen.getByTestId("settings-confirmed-idea")).toHaveTextContent(
      "No confirmed idea is stored.",
    );
  });
});

describe("F-01 — the controls write through the REAL endpoint", () => {
  it("saves pricing_model with ONE key and shows the SERVER's answer", async () => {
    await renderSettings();
    const next = { currency: "USD", plans: [{ name: "Single Candle", price: 28 }] };
    mockPut.mockResolvedValue({ ...CONFIG, pricing_model: next });

    fireEvent.change(pricingBox(), { target: { value: JSON.stringify(next) } });
    fireEvent.click(screen.getByRole("button", { name: "Save Pricing Model" }));

    await waitFor(() => expect(mockPut).toHaveBeenCalledTimes(1));
    const [path, body] = mockPut.mock.calls[0];
    expect(path).toBe("/config");
    expect(body).toEqual({ pricing_model: next });
    expect(Object.keys(body)).toEqual(["pricing_model"]);

    await waitFor(() =>
      expect(screen.getByTestId("settings-pricing-model-stored")).toHaveTextContent(
        "Single Candle — $28",
      ),
    );
    expect(screen.getByText(/SAVED — the server’s answer is shown above/)).toBeInTheDocument();
  });

  it("saves goals with ONE key", async () => {
    await renderSettings();
    const next = { launch_60d: ["Sell 300 units"], kpi_targets: { cac_max_usd: 22 } };
    mockPut.mockResolvedValue({ ...CONFIG, goals: next });

    fireEvent.change(goalsBox(), { target: { value: JSON.stringify(next) } });
    fireEvent.click(screen.getByRole("button", { name: "Save Goals" }));

    await waitFor(() => expect(mockPut).toHaveBeenCalledTimes(1));
    expect(mockPut.mock.calls[0][1]).toEqual({ goals: next });
  });

  it("saves github_repo, then clears it to JSON null (never the string 'null')", async () => {
    await renderSettings();
    mockPut.mockResolvedValue({ ...CONFIG, github_repo: "copper-pine/site" });

    fireEvent.change(repoBox(), { target: { value: "copper-pine/site" } });
    fireEvent.click(screen.getByRole("button", { name: "Save GitHub Repo" }));
    await waitFor(() => expect(mockPut).toHaveBeenCalledTimes(1));
    expect(mockPut.mock.calls[0][1]).toEqual({ github_repo: "copper-pine/site" });
    await waitFor(() =>
      expect(screen.getByTestId("settings-github-repo-stored")).toHaveTextContent(
        "Stored now: copper-pine/site",
      ),
    );

    mockPut.mockResolvedValue({ ...CONFIG, github_repo: null });
    fireEvent.change(repoBox(), { target: { value: "   " } });
    fireEvent.click(screen.getByRole("button", { name: "Save GitHub Repo" }));
    await waitFor(() => expect(mockPut).toHaveBeenCalledTimes(2));
    expect(mockPut.mock.calls[1][1]).toEqual({ github_repo: null });
    await waitFor(() =>
      expect(screen.getByTestId("settings-github-repo-stored")).toHaveTextContent("no value"),
    );
  });

  it("refuses a malformed JSON draft BEFORE any request — the form keeps the text", async () => {
    await renderSettings();

    fireEvent.change(pricingBox(), { target: { value: "{ not json" } });
    const save = screen.getByRole("button", { name: "Save Pricing Model" });
    expect(save).toBeDisabled();
    expect(screen.getByText(/This is not valid JSON/)).toBeInTheDocument();
    expect(mockPut).not.toHaveBeenCalled();

    // An ARRAY is valid JSON but not the server's declared object type.
    fireEvent.change(pricingBox(), { target: { value: "[1, 2]" } });
    expect(save).toBeDisabled();
    expect(screen.getByText(/JSON OBJECT/)).toBeInTheDocument();
    expect(pricingBox().value).toBe("[1, 2]");
  });

  it("renders a refused save by name and keeps the operator's text", async () => {
    await renderSettings();
    mockPut.mockRejectedValue(
      new ApiError(
        422,
        "pricing_model.skus[0].retail carries NaN — not a measurement",
        "E_VALIDATION",
        "send finite numbers (or null for unmeasured)",
      ),
    );

    fireEvent.change(pricingBox(), { target: { value: JSON.stringify({ skus: [{ name: "X", retail: 0 }] }) } });
    fireEvent.click(screen.getByRole("button", { name: "Save Pricing Model" }));

    const alert = await screen.findByTestId("settings-pricing-model-error");
    expect(alert).toHaveTextContent(
      "E_VALIDATION: pricing_model.skus[0].retail carries NaN — not a measurement — send finite numbers (or null for unmeasured)",
    );
    // Still the typed draft: no silent "saved", no silent revert.
    expect(JSON.parse(pricingBox().value)).toEqual({ skus: [{ name: "X", retail: 0 }] });
    expect(screen.getByTestId("settings-pricing-model-stored")).toHaveTextContent(
      "Fireside — $28 · Cedar & Sage — $28",
    );
  });

  it("sends nothing when the box is unchanged (Save is disabled)", async () => {
    await renderSettings();
    expect(screen.getByRole("button", { name: "Save Pricing Model" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Save Goals" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Save GitHub Repo" })).toBeDisabled();
  });
});

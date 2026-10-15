import { fireEvent, render, screen } from "@testing-library/react";

/**
 * WAVE V15 / W1 — item 4-06 FE half (#5 LOCKED): owner-profile Settings intake.
 *
 * Wire truth (proven against the shipping backend schemas — see the lane receipt's E1-lag proof):
 *  - `CompanyConfigUpdate` accepts `owner_skills` / `owner_goal` / `risk_tolerance` /
 *    `market_locale` (NOT NULL text, `null` NORMALISED to `''`), `owner_hours_per_week` /
 *    `owner_budget_usd` (nullable numerics — blank clears to NULL, NEVER 0), `channels_ready`
 *    (NOT NULL list, `null` -> `[]`);
 *  - `PUT /config` copies ONLY the provided keys (`model_dump(exclude_unset=True)`), so dirty
 *    owner fields must ride the SAME one-PUT save as the rest of the form, and untouched keys
 *    must never leave;
 *  - `CompanyConfigResponse` serves `''`/`null`/`[]` for "not set" — the boxes must render that
 *    state as EMPTY, never as 0 or "null".
 *
 * These pins fix that contract: values render honestly; dirty fields ride the PUT body with the
 * right JSON types; blank clears (null/[] — not 0); garbage numerics are refused by name with no
 * PUT; a refused save renders the server's named refusal.
 */

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

afterEach(() => {
  mockGet.mockReset();
  mockPut.mockReset();
});

const baseConfig: SettingsConfig = {
  name: "Polsia",
  mission: null,
  vision: null,
  description: null,
  target_market: null,
  value_prop: null,
  website_url: null,
  industry: null,
  product_type: null,
  timezone: null,
  daily_cycle_hour: null,
  owner_skills: "design",
  owner_hours_per_week: 20,
  owner_budget_usd: 1500,
  owner_goal: "first $10k MRR",
  risk_tolerance: "balanced",
  channels_ready: ["email", "seo"],
  market_locale: "US-IL",
};

function serveReads(config: SettingsConfig) {
  mockGet.mockImplementation((requestPath: string) => {
    if (requestPath === "/config") return Promise.resolve(config);
    if (requestPath === "/config/deepseek-key") {
      return Promise.resolve({ configured: false, source: "none" as const, masked: "" });
    }
    return Promise.reject(new Error(`unexpected GET ${requestPath}`));
  });
}

async function renderSettings(config: SettingsConfig = baseConfig) {
  serveReads(config);
  render(<SettingsPage />);
  await screen.findByRole("tab", { name: "Company Profile" });
}

const box = (label: string) => screen.getByLabelText(label) as HTMLInputElement;
const saveButton = () => screen.getByRole("button", { name: "Save Settings" });

describe("4-06 FE — owner-profile intake ('not set' semantics, one-PUT save)", () => {
  it("renders the server's values in the owner boxes", async () => {
    await renderSettings();

    expect(box("Owner Skills").value).toBe("design");
    expect(box("Owner Hours Per Week").value).toBe("20");
    expect(box("Owner Budget (USD)").value).toBe("1500");
    expect(box("Owner Goal").value).toBe("first $10k MRR");
    expect(box("Risk Tolerance").value).toBe("balanced");
    expect(box("Channels Ready").value).toBe("email, seo");
    expect(box("Market Locale").value).toBe("US-IL");
    // Numeric intent is declared, not guessed.
    expect(box("Owner Hours Per Week").getAttribute("inputmode")).toBe("numeric");
    expect(box("Owner Budget (USD)").getAttribute("inputmode")).toBe("numeric");
  });

  it("renders null/''/[] as EMPTY boxes — 'not set' is never 0, never the word null", async () => {
    await renderSettings({
      ...baseConfig,
      owner_skills: "",
      owner_hours_per_week: null,
      owner_budget_usd: null,
      channels_ready: [],
      market_locale: "",
    });

    expect(box("Owner Skills").value).toBe("");
    expect(box("Owner Hours Per Week").value).toBe("");
    expect(box("Owner Budget (USD)").value).toBe("");
    expect(box("Channels Ready").value).toBe("");
    expect(box("Market Locale").value).toBe("");
    expect(screen.queryByDisplayValue("0")).toBeNull();
    expect(screen.queryByDisplayValue("null")).toBeNull();
  });

  it("dirty owner fields ride the SAME one PUT with exact JSON types — untouched keys never leave", async () => {
    await renderSettings();
    const updated: SettingsConfig = {
      ...baseConfig,
      owner_skills: "copywriting",
      owner_hours_per_week: 12,
      owner_budget_usd: 250.5,
      channels_ready: ["twitter", "email", "seo"],
    };
    mockPut.mockResolvedValue(updated);

    fireEvent.change(box("Owner Skills"), { target: { value: "copywriting" } });
    fireEvent.change(box("Owner Hours Per Week"), { target: { value: "12" } });
    fireEvent.change(box("Owner Budget (USD)"), { target: { value: "250.5" } });
    fireEvent.change(box("Channels Ready"), { target: { value: "twitter, email, seo" } });

    fireEvent.click(saveButton());

    expect(mockPut).toHaveBeenCalledTimes(1);
    const [path, body] = mockPut.mock.calls[0] as [string, Record<string, unknown>];
    expect(path).toBe("/config");
    expect(body).toEqual({
      owner_skills: "copywriting",
      owner_hours_per_week: 12, // number, not "12"
      owner_budget_usd: 250.5, // number, decimal preserved
      channels_ready: ["twitter", "email", "seo"], // list, trimmed
    });
    // The response rebases the form: the save banner shows and Save disables again.
    expect(await screen.findByText("Settings saved successfully")).toBeInTheDocument();
    expect(saveButton()).toBeDisabled();
  });

  it("blank clears honestly: numerics leave as JSON null, the list as [] — NEVER 0", async () => {
    await renderSettings();
    mockPut.mockResolvedValue({
      ...baseConfig,
      owner_skills: "",
      owner_hours_per_week: null,
      owner_budget_usd: null,
      channels_ready: [],
    });

    fireEvent.change(box("Owner Skills"), { target: { value: "" } });
    fireEvent.change(box("Owner Hours Per Week"), { target: { value: "" } });
    fireEvent.change(box("Owner Budget (USD)"), { target: { value: "" } });
    fireEvent.change(box("Channels Ready"), { target: { value: "" } });

    fireEvent.click(saveButton());

    expect(mockPut).toHaveBeenCalledTimes(1);
    const [, body] = mockPut.mock.calls[0] as [string, Record<string, unknown>];
    expect(body).toEqual({
      owner_skills: "",
      owner_hours_per_week: null,
      owner_budget_usd: null,
      channels_ready: [],
    });
    expect(body.owner_hours_per_week).not.toBe(0);
    // Cleared boxes read back empty after the rebase.
    expect(await screen.findByText("Settings saved successfully")).toBeInTheDocument();
    expect(box("Owner Hours Per Week").value).toBe("");
    expect(box("Owner Budget (USD)").value).toBe("");
  });

  it("refuses a non-numeric / non-integer numeric by name and fires NO PUT", async () => {
    await renderSettings();

    fireEvent.change(box("Owner Hours Per Week"), { target: { value: "twelve" } });
    expect(saveButton()).toBeDisabled();
    expect(
      screen.getByText(/whole number of hours/i),
    ).toBeInTheDocument();

    fireEvent.change(box("Owner Hours Per Week"), { target: { value: "12.5" } });
    expect(saveButton()).toBeDisabled();
    expect(mockPut).not.toHaveBeenCalled();

    // Budget accepts decimals but not garbage.
    fireEvent.change(box("Owner Hours Per Week"), { target: { value: "12" } });
    fireEvent.change(box("Owner Budget (USD)"), { target: { value: "1,500" } });
    expect(saveButton()).toBeDisabled();
    expect(screen.getByText(/number for the budget/i)).toBeInTheDocument();
    expect(mockPut).not.toHaveBeenCalled();

    // A whole number + plain decimal repairs the form.
    fireEvent.change(box("Owner Budget (USD)"), { target: { value: "1500.50" } });
    expect(saveButton()).toBeEnabled();
  });

  it("renders a refused save with the server's own named refusal", async () => {
    await renderSettings();
    mockPut.mockRejectedValue(
      new ApiError(422, "owner_hours_per_week: Input should be a valid integer", "E_VALIDATION", "fix the field"),
    );

    fireEvent.change(box("Owner Skills"), { target: { value: "ops" } });
    fireEvent.click(saveButton());

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Save refused");
    expect(alert).toHaveTextContent("E_VALIDATION");
    expect(alert).toHaveTextContent("owner_hours_per_week");
  });
});

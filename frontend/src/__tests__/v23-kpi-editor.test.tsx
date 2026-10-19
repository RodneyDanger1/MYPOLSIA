import { fireEvent, render, screen, waitFor } from "@testing-library/react";

/**
 * WAVE V23 / L2 — the Settings KPI editor (root of the fixed D1).
 *
 * Wire truth (backend read-only, proven from `schemas/company.py` + `api/v1/config.py`):
 *  - `GET /config` serves `kpis` as a free-form `dict` (or null); a key the owner never entered is
 *    simply ABSENT and the dashboard renders `—` for it;
 *  - `PUT /config {"kpis": {...}}` copies only the provided top-level keys
 *    (`model_dump(exclude_unset=True)`), so the editor's write must carry exactly one key —
 *    `kpis` — and never touch the rest of the form;
 *  - the V22 honesty law: a BLANK box removes the key from the object. It is never sent as `0`,
 *    because 0 is a measurement and an absent key is not;
 *  - the server response is the only truth after a save: no optimistic state, no invented values.
 *
 * Every pin below fails against a stub that wires the boxes without these rules.
 */

jest.mock("@/lib/api", () => {
  const actual = jest.requireActual("@/lib/api");
  return {
    ...actual,
    api: { get: jest.fn(), post: jest.fn(), put: jest.fn(), delete: jest.fn(), patch: jest.fn() },
  };
});

import { ApiError, api } from "@/lib/api";
import SettingsPage, { type SettingsConfig } from "@/app/settings/page";

const mockGet = api.get as jest.Mock;
const mockPut = api.put as jest.Mock;

afterEach(() => {
  mockGet.mockReset();
  mockPut.mockReset();
  jest.restoreAllMocks();
  window.localStorage.clear();
  window.sessionStorage.clear();
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
  kpis: {
    mrr_usd: 1200,
    active_customers: 14,
    churn_rate: 2.5,
    cac_usd: 400,
    ltv_usd: 3000,
    nps: 42,
  },
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
  // The editor is seeded from the page's single GET /config — it fires no read of its own.
  await screen.findByLabelText("MRR (USD)");
}

const kpiBox = (label: string) => screen.getByLabelText(label) as HTMLInputElement;
const saveKpis = () => screen.getByRole("button", { name: "Save KPIs" });
const storedNote = () => screen.getByTestId("kpi-editor-stored");

function lastPutBody(): Record<string, unknown> {
  const call = mockPut.mock.calls[mockPut.mock.calls.length - 1] as [string, Record<string, unknown>];
  return call[1];
}

describe("V23/L2 — Settings KPI editor", () => {
  it("prefills the six numeric boxes from GET /config; absent keys read as — in the note", async () => {
    await renderSettings({
      ...baseConfig,
      kpis: { mrr_usd: 1200, active_customers: 14 },
    });

    expect(kpiBox("MRR (USD)").value).toBe("1200");
    expect(kpiBox("Active Customers").value).toBe("14");
    expect(kpiBox("Churn Rate (%)").value).toBe("");
    expect(kpiBox("CAC (USD)").value).toBe("");
    expect(kpiBox("LTV (USD)").value).toBe("");
    expect(kpiBox("NPS").value).toBe("");
    // Numeric intent is declared, not guessed; text boxes make a hostile entry visible.
    expect(kpiBox("MRR (USD)").getAttribute("inputmode")).toBe("decimal");

    // The editor's note states the stored truth per key — absent keys are the em dash, never 0.
    expect(storedNote()).toHaveTextContent("mrr_usd 1200");
    expect(storedNote()).toHaveTextContent("active_customers 14");
    expect(storedNote()).toHaveTextContent("churn_rate —");
    expect(storedNote()).toHaveTextContent("nps —");
    expect(screen.queryByDisplayValue("0")).toBeNull();

    // Copy is explicit that these are the owner's own measurements.
    expect(screen.getByTestId("kpi-editor-copy")).toHaveTextContent(/OWNER-ENTERED/i);
    expect(screen.getByTestId("kpi-editor-copy")).toHaveTextContent(/blank/i);
  });

  it("a blank box REMOVES the key from the PUT body — never sent as 0", async () => {
    await renderSettings();
    mockPut.mockResolvedValue({
      ...baseConfig,
      kpis: { active_customers: 14, churn_rate: 2.5, cac_usd: 400, ltv_usd: 3000, nps: 42 },
    });

    fireEvent.change(kpiBox("MRR (USD)"), { target: { value: "" } });
    fireEvent.click(saveKpis());

    await waitFor(() => expect(mockPut).toHaveBeenCalledTimes(1));
    expect(mockPut.mock.calls[0][0]).toBe("/config");
    const body = lastPutBody();
    // Exactly one top-level key: the editor never rewrites the rest of the form.
    expect(Object.keys(body)).toEqual(["kpis"]);
    expect(body.kpis).toEqual({
      active_customers: 14,
      churn_rate: 2.5,
      cac_usd: 400,
      ltv_usd: 3000,
      nps: 42,
    });
    expect("mrr_usd" in (body.kpis as Record<string, unknown>)).toBe(false);
  });

  it("sends a typed measurement as a JSON number", async () => {
    await renderSettings();
    mockPut.mockResolvedValue({ ...baseConfig, kpis: { ...baseConfig.kpis, mrr_usd: 4200.5 } });

    fireEvent.change(kpiBox("MRR (USD)"), { target: { value: "4200.5" } });
    fireEvent.click(saveKpis());

    await waitFor(() => expect(mockPut).toHaveBeenCalledTimes(1));
    const kpis = lastPutBody().kpis as Record<string, unknown>;
    expect(kpis.mrr_usd).toBe(4200.5);
    expect(typeof kpis.mrr_usd).toBe("number");
    // The untouched keys still ride the object as numbers.
    expect(kpis.active_customers).toBe(14);
  });

  it("reflects the SERVER's response after save — no optimistic state", async () => {
    await renderSettings();
    // The server answers a value different from what was typed: the server's answer wins.
    mockPut.mockResolvedValue({
      ...baseConfig,
      kpis: { mrr_usd: 9999, active_customers: 3, churn_rate: 1, cac_usd: 2, ltv_usd: 4, nps: 5 },
    });

    fireEvent.change(kpiBox("MRR (USD)"), { target: { value: "123" } });
    fireEvent.click(saveKpis());

    expect(await screen.findByText("KPIs saved")).toBeInTheDocument();
    expect(kpiBox("MRR (USD)").value).toBe("9999");
    expect(kpiBox("Active Customers").value).toBe("3");
    expect(storedNote()).toHaveTextContent("mrr_usd 9999");
    // The form is rebased on the server answer: Save is disabled again.
    expect(saveKpis()).toBeDisabled();
  });

  it("clear-all writes {} through the same path and reflects the server's empty object", async () => {
    await renderSettings();
    mockPut.mockResolvedValue({ ...baseConfig, kpis: {} });

    fireEvent.click(screen.getByRole("button", { name: "Clear all KPIs" }));

    await waitFor(() => expect(mockPut).toHaveBeenCalledTimes(1));
    expect(mockPut.mock.calls[0][0]).toBe("/config");
    expect(lastPutBody()).toEqual({ kpis: {} });
    expect(await screen.findByText("KPIs saved")).toBeInTheDocument();
    for (const label of [
      "MRR (USD)",
      "Active Customers",
      "Churn Rate (%)",
      "CAC (USD)",
      "LTV (USD)",
      "NPS",
    ]) {
      expect(kpiBox(label).value).toBe("");
    }
    // The server's empty object is reflected: every stored reading is the em dash.
    expect(storedNote()).toHaveTextContent("mrr_usd —");
    expect(storedNote()).toHaveTextContent("nps —");
  });

  it("a refused save keeps the form AND names the error inline", async () => {
    await renderSettings();
    mockPut.mockRejectedValue(
      new ApiError(
        422,
        "kpis.mrr_usd carries NaN/Infinity — not valid JSON and not a measurement",
        "E_VALIDATION",
        "send finite numbers (or null for unmeasured)",
      ),
    );

    fireEvent.change(kpiBox("MRR (USD)"), { target: { value: "77" } });
    fireEvent.click(saveKpis());

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("KPI save refused");
    expect(alert).toHaveTextContent("E_VALIDATION");
    expect(alert).toHaveTextContent("kpis.mrr_usd");
    // The form survives the refusal untouched, and the same save can be retried.
    expect(kpiBox("MRR (USD)").value).toBe("77");
    expect(saveKpis()).toBeEnabled();
    expect(screen.queryByText("KPIs saved")).toBeNull();
  });

  it("refuses a non-numeric entry client-side by name — no PUT fires", async () => {
    await renderSettings();

    fireEvent.change(kpiBox("MRR (USD)"), { target: { value: "twelve" } });
    expect(saveKpis()).toBeDisabled();
    expect(screen.getByText(/Enter a number for MRR/i)).toBeInTheDocument();
    expect(mockPut).not.toHaveBeenCalled();

    fireEvent.change(kpiBox("MRR (USD)"), { target: { value: "1,500" } });
    expect(saveKpis()).toBeDisabled();
    expect(screen.getByText(/Enter a number for MRR/i)).toBeInTheDocument();
    expect(mockPut).not.toHaveBeenCalled();

    // A plain number repairs the form.
    fireEvent.change(kpiBox("MRR (USD)"), { target: { value: "1500" } });
    expect(saveKpis()).toBeEnabled();
    expect(screen.queryByText(/Enter a number for MRR/i)).toBeNull();
  });

  it("does not invent bounds: a huge finite number is sent and the server decides", async () => {
    await renderSettings();
    mockPut.mockResolvedValue({ ...baseConfig, kpis: { ...baseConfig.kpis, mrr_usd: 1e21 } });

    fireEvent.change(kpiBox("MRR (USD)"), { target: { value: "999999999999999999999" } });
    expect(saveKpis()).toBeEnabled();
    fireEvent.click(saveKpis());

    await waitFor(() => expect(mockPut).toHaveBeenCalledTimes(1));
    const kpis = lastPutBody().kpis as Record<string, unknown>;
    expect(kpis.mrr_usd).toBe(1e21);
    expect(Number.isFinite(kpis.mrr_usd as number)).toBe(true);
  });

  it("uses no browser storage", async () => {
    const setItem = jest.spyOn(Storage.prototype, "setItem");
    await renderSettings();
    mockPut.mockResolvedValue({ ...baseConfig, kpis: { ...baseConfig.kpis, mrr_usd: 500 } });

    fireEvent.change(kpiBox("MRR (USD)"), { target: { value: "500" } });
    fireEvent.click(saveKpis());
    await waitFor(() => expect(mockPut).toHaveBeenCalledTimes(1));
    await screen.findByText("KPIs saved");

    expect(setItem).not.toHaveBeenCalled();
    expect(window.localStorage.length).toBe(0);
    expect(window.sessionStorage.length).toBe(0);
  });
});

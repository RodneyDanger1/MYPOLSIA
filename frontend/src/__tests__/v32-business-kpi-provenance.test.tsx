/**
 * WAVE 4 / F-W3-1 (TS-4) — the Business panel must never present CONFIGURED KPIs as measured.
 *
 * THE DEFECT (measured live on the wave-3 build): `GET /dashboard/summary` returns
 * `kpis: {mrr_usd: 0, active_customers: 0}` (the OWNER'S Settings values) plus the TS-4
 * provenance block, and the panel rendered those under the bare labels "MRR / Active customers /
 * Churn rate" — so a configured zero read as a measured `$0.00` while `GET /finance/summary`
 * correctly answered `mrr_cents: null` on the same stack.
 *
 * WHAT THIS PINS:
 *   * measured values come from `measured_finance` (cents -> dollars) and an unmeasured one is the
 *     em dash — never a zero;
 *   * configured values are shown only under labels that say "configured", inside a section headed
 *     "Settings values, not measured", with a sentence naming the server's declared provenance;
 *   * a configured `$0.00` is therefore never the only reading on a measured-looking row;
 *   * a server that predates TS-4 says exactly that instead of implying its bag was measured.
 */
import { render, screen } from "@testing-library/react";

jest.mock("@/lib/api", () => {
  const actual = jest.requireActual("@/lib/api");
  return { ...actual, api: { get: jest.fn() } };
});

import { api } from "@/lib/api";
import { BusinessPanel } from "@/components/dashboard/BusinessPanel";

const mockGet = api.get as jest.Mock;

const BASE = {
  tasks_today_total: 0,
  tasks_today_completed: 0,
  tasks_today_pending: 0,
  tasks_today_failed: 0,
  active_agents: [],
  last_report_date: null,
};

function answerWith(summary: unknown): void {
  mockGet.mockImplementation((path: string) => {
    if (path === "/dashboard/summary") return Promise.resolve(summary);
    if (path === "/finance/planning") return Promise.resolve({ margin_template: null, reasons: {} });
    return Promise.reject(new Error(`unexpected GET ${path}`));
  });
}

beforeEach(() => {
  mockGet.mockReset();
});

it("configured zeros are labelled configured and the measured rows show the em dash", async () => {
  answerWith({
    ...BASE,
    kpis: { mrr_usd: 0, active_customers: 0, churn_rate: 0 },
    kpis_provenance: "configured",
    measured_finance: {
      mrr_cents: null,
      active_subscribers: null,
      last_snapshot_date: "2026-10-28",
      provenance: "partial",
    },
  });

  render(<BusinessPanel />);

  // The measured rows are UNMEASURED — the dash, never $0.00 / 0.
  expect(await screen.findByTestId("measured-mrr")).toHaveTextContent("—");
  expect(screen.getByTestId("measured-active-customers")).toHaveTextContent("—");
  expect(screen.getByTestId("measured-provenance")).toHaveTextContent(/partial/);
  expect(screen.getByTestId("measured-provenance")).toHaveTextContent(/2026-10-28/);
  expect(screen.getByTestId("measured-unmeasured-note")).toHaveTextContent(/not a zero/i);

  // The configured bag is still shown — but only under configured labels.
  expect(screen.getByTestId("configured-mrr")).toHaveTextContent("$0.00");
  expect(screen.getByTestId("configured-active-customers")).toHaveTextContent("0");
  expect(screen.getByTestId("configured-churn")).toHaveTextContent("0%");
  expect(screen.getByText(/Settings values, not measured/i)).toBeInTheDocument();
  expect(screen.getByTestId("kpi-provenance-note")).toHaveTextContent(/declares these values configured/i);

  // The never-again pin: nothing on this panel claims a measured $0.00.
  expect(screen.queryByText(/\$0\.00 measured/i)).toBeNull();
  const measured = screen.getByTestId("measured-mrr");
  expect(measured).not.toHaveTextContent("$0.00");
});

it("measured values lead; the configured bag keeps its own labelled rows", async () => {
  answerWith({
    ...BASE,
    kpis: { mrr_usd: 9999, active_customers: 99, churn_rate: 4.5 },
    kpis_provenance: "configured",
    measured_finance: {
      mrr_cents: 250000,
      active_subscribers: 3,
      last_snapshot_date: "2026-10-27",
      provenance: "complete",
    },
  });

  render(<BusinessPanel />);

  expect(await screen.findByTestId("measured-mrr")).toHaveTextContent("$2,500.00");
  expect(screen.getByTestId("measured-active-customers")).toHaveTextContent("3");
  expect(screen.getByTestId("measured-provenance")).toHaveTextContent(/complete/);
  expect(screen.queryByTestId("measured-unmeasured-note")).toBeNull();
  // The configured values are NOT laundered into the measured rows.
  expect(screen.getByTestId("measured-mrr")).not.toHaveTextContent("$9,999.00");
  expect(screen.getByTestId("configured-mrr")).toHaveTextContent("$9,999.00");
  expect(screen.getByTestId("configured-active-customers")).toHaveTextContent("99");
  expect(screen.getByTestId("configured-churn")).toHaveTextContent("4.5%");
});

it("a pre-TS-4 server says it served no measured block — it is never implied", async () => {
  answerWith({
    ...BASE,
    kpis: { mrr_usd: 1234, active_customers: 12 },
  });

  render(<BusinessPanel />);

  expect(await screen.findByTestId("measured-finance-unavailable")).toHaveTextContent(
    /did not serve a measured finance block/,
  );
  expect(screen.getByTestId("measured-mrr")).toHaveTextContent("—");
  expect(screen.getByTestId("measured-active-customers")).toHaveTextContent("—");
  expect(screen.getByTestId("kpi-provenance-note")).toHaveTextContent(
    /did not declare the KPI provenance/,
  );
  // The configured values still render, under configured labels.
  expect(screen.getByTestId("configured-mrr")).toHaveTextContent("$1,234.00");
  expect(screen.getByTestId("configured-active-customers")).toHaveTextContent("12");
});

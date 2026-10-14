/**
 * Wave V8b / seat l3-media-docs — the Media card, the Documents overlay (ALL / DOCUMENTS /
 * REPORTS over the V8/c5 reports list) and the Settings Pause/Resume Switch.
 *
 * The API module is mocked the way the sibling suites do it (u1/u2/u3/u7): the surfaces are
 * pinned on their behaviour — what they read, what they send, which controls do real work.
 * Wire truth read from the backend before writing these tests:
 *   GET /api/v1/dashboard/reports?limit=N → {items: [DailyReportResponse…], total}
 *     (`backend/app/api/v1/dashboard.py:145`, V8/c5; total is the TABLE's count)
 *   DailyReportResponse fields (`backend/app/schemas/report.py:34`): id, report_date,
 *     morning_plan (nullable), evening_summary, tasks_planned, tasks_completed, tasks_failed,
 *     updated_at (nullable)
 *   dispatch_paused rides GET/PUT /config (`backend/app/schemas/company.py:70,137`) and the
 *     pause gate refuses dispatch by name (`app/agents/crew_factory.py:222`, E_POLICY_DENIED).
 */
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

jest.mock("@/lib/api", () => {
  const actual = jest.requireActual("@/lib/api");
  return {
    ...actual,
    api: { get: jest.fn(), post: jest.fn(), put: jest.fn(), delete: jest.fn() },
  };
});

import { api } from "@/lib/api";
import { MediaCard } from "@/components/dashboard/MediaCard";
import { DailyReportPanel } from "@/components/dashboard/DailyReportPanel";
import SettingsPage, { type SettingsConfig } from "@/app/settings/page";

const mockGet = api.get as jest.Mock;
const mockPut = api.put as jest.Mock;

afterEach(() => {
  mockGet.mockReset();
  mockPut.mockReset();
});

const REPORTS_PATH = "/dashboard/reports?limit=50";
const DOCUMENTS_LINE =
  "No documents store in this deployment — daily reports live under REPORTS.";

function reportsRow(over: Record<string, unknown> = {}) {
  return {
    id: 1,
    report_date: "2026-10-13",
    morning_plan: "Plan: ship the overlay, then the pause switch.",
    evening_summary: null,
    tasks_planned: 4,
    tasks_completed: 3,
    tasks_failed: 1,
    updated_at: null,
    ...over,
  };
}

/** The panel's own daily read + the overlay's list read; an unlisted path fails loudly. */
function serveDashboardReads(reports: unknown) {
  mockGet.mockImplementation((path: string) => {
    if (path === "/dashboard/reports/daily") return Promise.resolve(null);
    if (path === REPORTS_PATH) {
      return reports instanceof Error ? Promise.reject(reports) : Promise.resolve(reports);
    }
    return Promise.reject(new Error(`unexpected GET ${path}`));
  });
}

/** Render the panel, open the overlay the way a person does (userEvent moves focus on click). */
async function openOverlay() {
  render(<DailyReportPanel />);
  const viewAll = await screen.findByRole("button", { name: "VIEW ALL" });
  expect(viewAll).toHaveAttribute("aria-expanded", "false");
  const user = userEvent.setup();
  await user.click(viewAll);
  const dialog = await screen.findByRole("dialog", { name: "Documents" });
  return { viewAll, dialog, user };
}

// -------------------------------------------------------------------------------------------
// A — the Media card (ui-gap §8 honest minimum)
// -------------------------------------------------------------------------------------------

describe("MediaCard (V8b/L3)", () => {
  it("renders the honest empty line — and contains ZERO buttons", () => {
    const { container } = render(<MediaCard />);

    expect(
      screen.getByText("No media yet — this deployment ships no image/video generator yet."),
    ).toBeInTheDocument();
    // The whole point of the honest minimum: no dead IMAGE / VIDEO affordances.
    expect(within(container).queryAllByRole("button")).toHaveLength(0);
    expect(screen.queryByText("NEW CAPABILITY")).toBeNull();
  });
});

// -------------------------------------------------------------------------------------------
// B — the Daily Report panel opener + the Documents overlay
// -------------------------------------------------------------------------------------------

describe("Documents overlay (V8b/L3)", () => {
  it("VIEW ALL opens the dialog, triggers the list read, and the counter shows 2 OF 6", async () => {
    serveDashboardReads({
      items: [reportsRow(), reportsRow({ id: 2, report_date: "2026-10-12" })],
      total: 6,
    });

    const { viewAll, dialog } = await openOverlay();

    expect(viewAll).toHaveAttribute("aria-expanded", "true");
    expect(mockGet).toHaveBeenCalledWith(REPORTS_PATH);
    expect(await within(dialog).findByText("2 OF 6")).toBeInTheDocument();
  });

  it("tabs: REPORTS keeps the rows; DOCUMENTS shows only the honest line; ALL shows rows + the line", async () => {
    serveDashboardReads({ items: [reportsRow()], total: 1 });

    const { dialog } = await openOverlay();
    await within(dialog).findByText("1 OF 1");

    // ALL (default): the reports rows PLUS the standing documents-position line.
    expect(within(dialog).getByText("2026-10-13")).toBeInTheDocument();
    expect(within(dialog).getByText(DOCUMENTS_LINE)).toBeInTheDocument();

    fireEvent.click(within(dialog).getByRole("tab", { name: "REPORTS" }));
    expect(within(dialog).getByText("2026-10-13")).toBeInTheDocument();
    expect(within(dialog).queryByText(DOCUMENTS_LINE)).toBeNull();

    fireEvent.click(within(dialog).getByRole("tab", { name: "DOCUMENTS" }));
    expect(within(dialog).getByText(DOCUMENTS_LINE)).toBeInTheDocument();
    expect(within(dialog).queryByText("2026-10-13")).toBeNull();
  });

  it("rows: a null morning_plan renders NO excerpt; the counts line carries the three numbers", async () => {
    serveDashboardReads({
      items: [
        reportsRow({ id: 1, report_date: "2026-10-13", morning_plan: null }),
        reportsRow({
          id: 2,
          report_date: "2026-10-12",
          morning_plan: "Write the weekly launch note.",
        }),
      ],
      total: 2,
    });

    const { dialog } = await openOverlay();

    const first = await within(dialog).findByText("2026-10-13");
    const nullRow = first.closest("li") as HTMLElement;
    expect(within(nullRow).getByText("4 planned · 3 done · 1 failed")).toBeInTheDocument();
    // The null plan renders NOTHING — no filler line, no dash (one <p> only for a real excerpt).
    expect(nullRow.querySelector("p")).toBeNull();

    const excerpt = within(dialog).getByText("Write the weekly launch note.");
    expect(excerpt.closest("li")).not.toBe(nullRow);
  });

  it("Escape closes the dialog, focus returns to VIEW ALL, and a reopen reads fresh", async () => {
    serveDashboardReads({ items: [reportsRow()], total: 1 });

    const { viewAll, user } = await openOverlay();
    await screen.findByText("1 OF 1");

    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(viewAll).toHaveFocus();

    await user.click(viewAll);
    await screen.findByRole("dialog", { name: "Documents" });
    const listReads = mockGet.mock.calls.filter(([path]) => path === REPORTS_PATH).length;
    expect(listReads).toBe(2);
  });

  it("keeps Tab inside the dialog (Shift+Tab from the first control wraps to the last)", async () => {
    serveDashboardReads({ items: [reportsRow()], total: 1 });

    const { dialog } = await openOverlay();
    await within(dialog).findByText("1 OF 1");

    // Focus lands on the ✕ on open (mirroring the task sheet); Shift+Tab must wrap, not leave.
    const close = within(dialog).getByRole("button", { name: "Close documents" });
    expect(close).toHaveFocus();
    fireEvent.keyDown(close, { key: "Tab", shiftKey: true });
    expect(within(dialog).getByRole("tab", { name: "ALL" })).toHaveFocus();
  });

  it("a failed list read renders the named error inside the dialog", async () => {
    serveDashboardReads(new Error("E_DB: could not read daily reports"));

    const { dialog } = await openOverlay();

    const alert = await within(dialog).findByRole("alert");
    expect(alert).toHaveTextContent(
      "Could not load the reports list: E_DB: could not read daily reports",
    );
  });

  it("cap honesty: a total beyond the 50-row read SAYS so", async () => {
    const items = Array.from({ length: 50 }, (_, index) =>
      reportsRow({ id: index + 1, report_date: `2026-09-${String(index + 1).padStart(2, "0")}` }),
    );
    serveDashboardReads({ items, total: 120 });

    const { dialog } = await openOverlay();

    expect(await within(dialog).findByText("50 OF 120 — showing the newest 50")).toBeInTheDocument();
  });
});

// -------------------------------------------------------------------------------------------
// C — the Settings Pause/Resume Switch (one-PUT boolean idiom)
// -------------------------------------------------------------------------------------------

const BASE_CONFIG: SettingsConfig = {
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
};

function serveConfig(config: SettingsConfig) {
  mockGet.mockImplementation((path: string) => {
    if (path === "/config") return Promise.resolve(config);
    return Promise.reject(new Error(`unexpected GET ${path}`));
  });
}

describe("Settings pause switch (V8b/L3)", () => {
  it("dispatch_paused true renders CHECKED; a toggle rides the one-PUT save as a boolean", async () => {
    serveConfig({ ...BASE_CONFIG, dispatch_paused: true });
    mockPut.mockResolvedValue({ ...BASE_CONFIG, dispatch_paused: false });

    render(<SettingsPage />);

    const toggle = await screen.findByRole("switch", { name: "Pause all dispatches" });
    expect(toggle).toHaveAttribute("aria-checked", "true");

    // The helper names the REAL dispatch refusal (E_POLICY_DENIED — crew_factory.py:222).
    expect(screen.getByText(/refused by name \(E_POLICY_DENIED\)/)).toBeInTheDocument();

    fireEvent.click(toggle); // resume: true -> false
    expect(toggle).toHaveAttribute("aria-checked", "false");

    fireEvent.click(screen.getByRole("button", { name: "Save Settings" }));

    await waitFor(() => expect(mockPut).toHaveBeenCalledTimes(1));
    const [path, body] = mockPut.mock.calls[0];
    expect(path).toBe("/config");
    expect(body).toEqual({ dispatch_paused: false });
    expect(typeof body.dispatch_paused).toBe("boolean");
  });

  it("a fixture WITHOUT dispatch_paused renders unchecked and does not crash", async () => {
    serveConfig({ ...BASE_CONFIG });

    render(<SettingsPage />);

    const toggle = await screen.findByRole("switch", { name: "Pause all dispatches" });
    expect(toggle).toHaveAttribute("aria-checked", "false");
  });
});

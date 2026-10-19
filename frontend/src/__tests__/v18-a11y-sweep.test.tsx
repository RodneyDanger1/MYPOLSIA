/**
 * WAVE V18 / seat L4 — DEEP A11Y SWEEP pins (the wave's frontend-wide pass).
 *
 * Each pin is RED against the pre-fix tree (raw/01-red.txt) and names one real defect found by
 * walking the interactive surface:
 *
 *   D-1  DataTable tables had NO accessible name  -> every DataTable on /usage, /settings and the
 *        ops panel was announced as an unnamed "table".
 *   D-2  FinanceListPanel tables had no accessible name either (same defect, the finance grammar).
 *   D-3  TaskBoard's column disclosure carried `aria-expanded` but no `aria-controls`, so the
 *        relationship between the TO DO button and the list it collapses was unreported.
 *   D-4  NewTaskDialog's invalid priority field announced an alert that was not ASSOCIATED with
 *        the input (`aria-invalid` / `aria-describedby` missing) — a screen-reader user heard an
 *        alert with no field context.
 *   D-9  NewTaskDialog is a modal with no Tab trap: focus could walk onto the page behind it.
 *   D-10 ActivityStrip's log dialog had the same hole (Escape only).
 *
 * The phone console's own two pins live in `v18-phone-media.test.tsx` (same rendered page).
 */
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";

jest.mock("@/lib/api", () => {
  const actual = jest.requireActual("@/lib/api");
  return { ...actual, api: { get: jest.fn() }, API_KEY: "test-key" };
});

// The activity strip reads the ONE feed hook; the dialog's focus contract is what this pin tests.
jest.mock("@/hooks/useActivityFeed", () => ({
  useActivityFeed: () => ({ events: [], status: "live", error: null, attempts: 0 }),
}));

import { api } from "@/lib/api";
import { DataTable } from "@/components/ui";
import { FinanceListPanel } from "@/components/finance/FinanceListPanel";
import { TaskBoard } from "@/components/tasks/TaskBoard";
import { NewTaskDialog } from "@/components/tasks/NewTaskDialog";
import { DailyReportPanel } from "@/components/dashboard/DailyReportPanel";
import { ActivityStrip } from "@/components/shell/ActivityStrip";

const mockApiGet = api.get as jest.Mock;

beforeEach(() => {
  mockApiGet.mockReset();
});

it("D-1: a labelled DataTable exposes that name as its caption (not an unnamed table)", () => {
  render(
    <DataTable
      label="Usage runs"
      columns={[{ key: "agent", header: "Agent", render: (row: { agent: string }) => row.agent }]}
      rows={[{ agent: "researcher" }]}
      rowKey={(row) => row.agent}
      empty={<p>none</p>}
    />,
  );

  const table = screen.getByRole("table", { name: "Usage runs" });
  expect(table).toBeInTheDocument();
  // The caption is the table's own name, not a floating paragraph.
  expect(table.querySelector("caption")).toHaveTextContent("Usage runs");
});

it("D-2: a finance list table carries the panel's title as its accessible name", async () => {
  mockApiGet.mockResolvedValue([{ id: "a1", amount: "10.00" }]);

  render(
    <FinanceListPanel<{ id: string; amount: string }>
      title="Revenue snapshots"
      testId="revenue"
      path="/finance/revenue?limit=50"
      limit={50}
      noun="snapshot"
      errorLabel="the revenue list"
      emptyText="No revenue snapshots."
      rowKey={(row) => row.id}
      columns={[
        { key: "id", label: "Id", value: (row) => row.id },
        { key: "amount", label: "Amount", value: (row) => row.amount },
      ]}
      reloadKey={0}
    />,
  );

  await waitFor(() => expect(screen.getByTestId("revenue-table")).toBeInTheDocument());
  expect(screen.getByRole("table", { name: "Revenue snapshots" })).toBeInTheDocument();
});

it("D-3: the board's column disclosure names the region it collapses via aria-controls", () => {
  render(<TaskBoard rows={[]} onOpen={() => {}} />);

  const toDo = screen.getByRole("button", { name: "TO DO" });
  expect(toDo).toHaveAttribute("aria-expanded", "true");
  const controls = toDo.getAttribute("aria-controls");
  expect(controls).toBeTruthy();
  const region = document.getElementById(controls as string);
  expect(region).not.toBeNull();

  fireEvent.click(toDo);
  expect(toDo).toHaveAttribute("aria-expanded", "false");
  // A dangling reference is invalid ARIA: collapsed, the region is gone and so is the pointer.
  expect(toDo).not.toHaveAttribute("aria-controls");
  expect(document.getElementById(controls as string)).toBeNull();
});

it("D-4: the invalid priority field owns its error (aria-invalid + aria-describedby)", () => {
  render(<NewTaskDialog onClose={() => {}} />);

  const priority = screen.getByLabelText(/priority/i) as HTMLInputElement;
  expect(priority).not.toHaveAttribute("aria-invalid");

  // A number input drops "abc"; "1.5" is accepted by the element and refused by the form's
  // integer rule — the state a screen-reader user actually meets.
  fireEvent.change(priority, { target: { value: "1.5" } });

  const alert = screen.getByRole("alert");
  expect(alert).toHaveTextContent("whole number");
  expect(priority).toHaveAttribute("aria-invalid", "true");
  expect(priority.getAttribute("aria-describedby")).toBe(alert.getAttribute("id"));
  expect(alert.getAttribute("id")).toBeTruthy();

  // D-5: the two fields the labels call "(required)" are programmatically required too.
  expect(screen.getByLabelText("Title (required)")).toHaveAttribute("aria-required", "true");
  expect(screen.getByLabelText("Agent (required)")).toHaveAttribute("aria-required", "true");
});

it("D-6: VIEW ALL announces that it opens a dialog (not an inline disclosure)", async () => {
  mockApiGet.mockResolvedValue(null);

  render(<DailyReportPanel />);

  const viewAll = await screen.findByRole("button", { name: "VIEW ALL" });
  expect(viewAll).toHaveAttribute("aria-haspopup", "dialog");
});

it("D-9: the new-task dialog traps Tab — focus cannot walk onto the page behind it", () => {
  render(<NewTaskDialog onClose={() => {}} />);

  fireEvent.change(screen.getByLabelText("Title (required)"), { target: { value: "Ship it" } });
  fireEvent.change(screen.getByLabelText("Agent (required)"), { target: { value: "orchestrator" } });

  const create = screen.getByRole("button", { name: "Create task" });
  const close = screen.getByRole("button", { name: "Close new task dialog" });
  create.focus();
  fireEvent.keyDown(create, { key: "Tab" });
  expect(close).toHaveFocus();
});

it("D-10: the activity log dialog traps Tab in both directions", async () => {
  render(<ActivityStrip />);

  fireEvent.click(screen.getByRole("button", { name: "Log" }));
  const dialog = await screen.findByTestId("activity-log");
  const close = within(dialog).getByRole("button", { name: "Close activity log" });
  const download = within(dialog).getByRole("button", { name: "Download .log" });

  expect(close).toHaveFocus();
  download.focus();
  fireEvent.keyDown(download, { key: "Tab" });
  expect(close).toHaveFocus();

  fireEvent.keyDown(close, { key: "Tab", shiftKey: true });
  expect(download).toHaveFocus();
});

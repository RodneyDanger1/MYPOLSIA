import { fireEvent, render, screen, within } from "@testing-library/react";

import {
  DataTable,
  RadioList,
  Switch,
  Tabs,
  type Column,
  type DataTableProps,
  type RadioListProps,
  type SwitchProps,
  type TabsProps,
} from "@/components/ui";
import * as uiBarrel from "@/components/ui";

/**
 * Wave V7 / S1 — the four shared primitives (frozen signatures in the wave plan §6): Switch,
 * RadioList, Tabs, DataTable, plus the `@/components/ui` barrel exports (T01..T05).
 *
 * These tests hold the frozen contract stage-B consumers code against: names, shapes,
 * keyboard behaviour, and the barrel export path. Visual recipes live in globals.css and
 * are not asserted here (class strings are free to evolve; signatures are not).
 */

// ---- fixtures ---------------------------------------------------------------

type Frequency = "once" | "daily" | "weekly";

const FREQUENCIES: { value: Frequency; label: string; note?: string }[] = [
  { value: "once", label: "Once", note: "one run now" },
  { value: "daily", label: "Daily", note: "30 tasks/mo" },
  { value: "weekly", label: "Weekly", note: "4 tasks/mo" },
];

type Filter = "all" | "documents" | "reports";

const FILTERS: { value: Filter; label: string }[] = [
  { value: "all", label: "All" },
  { value: "documents", label: "Documents" },
  { value: "reports", label: "Reports" },
];

type RunRow = { id: string; title: string; cost: number | null };

const RUN_ROWS: RunRow[] = [
  { id: "run-1", title: "First run", cost: 12 },
  { id: "run-2", title: "Second run", cost: null },
];

const RUN_COLUMNS: Column<RunRow>[] = [
  { key: "title", header: "Title", render: (row) => row.title },
  { key: "cost", header: "Cost", render: (row) => (row.cost === null ? "—" : String(row.cost)) },
];

// ---- V7-T01 Switch ----------------------------------------------------------

describe("V7-T01 Switch", () => {
  it("renders both segment labels; role=switch + aria-checked=false when unchecked", () => {
    render(<Switch checked={false} onChange={() => {}} label="Night task" />);

    const control = screen.getByRole("switch", { name: "Night task" });
    expect(control).toHaveAttribute("aria-checked", "false");
    expect(control).toHaveTextContent("ON");
    expect(control).toHaveTextContent("OFF");
  });

  it("click toggles — onChange called with the toggled value (false -> true)", () => {
    const onChange = jest.fn();
    render(<Switch checked={false} onChange={onChange} label="Night task" />);

    fireEvent.click(screen.getByRole("switch", { name: "Night task" }));
    expect(onChange).toHaveBeenCalledTimes(1);
    expect(onChange).toHaveBeenCalledWith(true);
  });

  it("aria-checked=true when checked; toggles off via onChange(false); id passes through", () => {
    const onChange = jest.fn();
    render(<Switch checked onChange={onChange} label="Night task" id="night-task" />);

    const control = screen.getByRole("switch", { name: "Night task" });
    expect(control).toHaveAttribute("aria-checked", "true");
    expect(control).toHaveAttribute("id", "night-task");
    fireEvent.click(control);
    expect(onChange).toHaveBeenCalledWith(false);
  });

  it("disabled carries the disabled attribute and blocks interaction (no onChange)", () => {
    const onChange = jest.fn();
    render(<Switch checked={false} onChange={onChange} label="Night task" disabled />);

    const control = screen.getByRole("switch", { name: "Night task" });
    expect(control).toBeDisabled();
    fireEvent.click(control);
    expect(onChange).not.toHaveBeenCalled();
  });
});

// ---- V7-T02 RadioList -------------------------------------------------------

describe("V7-T02 RadioList", () => {
  it("renders options in a radiogroup, marks the current value checked, shows notes", () => {
    render(<RadioList options={FREQUENCIES} value="daily" onChange={() => {}} name="Frequency" />);

    expect(screen.getByRole("radiogroup", { name: "Frequency" })).toBeInTheDocument();
    expect(screen.getAllByRole("radio")).toHaveLength(3);
    expect(screen.getByRole("radio", { name: /Daily/ })).toHaveAttribute("aria-checked", "true");
    expect(screen.getByRole("radio", { name: /Once/ })).toHaveAttribute("aria-checked", "false");
    expect(screen.getByText("30 tasks/mo")).toBeInTheDocument();
  });

  it("click selects an option via onChange(value)", () => {
    const onChange = jest.fn();
    render(<RadioList options={FREQUENCIES} value="once" onChange={onChange} name="Frequency" />);

    fireEvent.click(screen.getByRole("radio", { name: /Weekly/ }));
    expect(onChange).toHaveBeenCalledWith("weekly");
  });

  it("ArrowDown/ArrowUp move the selection (wrapping both directions)", () => {
    const onChange = jest.fn();
    render(<RadioList options={FREQUENCIES} value="once" onChange={onChange} name="Frequency" />);

    fireEvent.keyDown(screen.getByRole("radio", { name: /Once/ }), { key: "ArrowDown" });
    expect(onChange).toHaveBeenLastCalledWith("daily");

    onChange.mockClear();
    fireEvent.keyDown(screen.getByRole("radio", { name: /Once/ }), { key: "ArrowUp" });
    expect(onChange).toHaveBeenLastCalledWith("weekly");

    onChange.mockClear();
    fireEvent.keyDown(screen.getByRole("radio", { name: /Weekly/ }), { key: "ArrowDown" });
    expect(onChange).toHaveBeenLastCalledWith("once");
  });

  it("Space selects the focused option", () => {
    const onChange = jest.fn();
    render(<RadioList options={FREQUENCIES} value="once" onChange={onChange} name="Frequency" />);

    fireEvent.keyDown(screen.getByRole("radio", { name: /Daily/ }), { key: " " });
    expect(onChange).toHaveBeenCalledWith("daily");
  });
});

// ---- V7-T03 Tabs ------------------------------------------------------------

describe("V7-T03 Tabs", () => {
  it("renders a tablist; aria-selected + roving tabindex only on the active tab", () => {
    render(<Tabs items={FILTERS} value="documents" onChange={() => {}} ariaLabel="Document filters" />);

    expect(screen.getByRole("tablist", { name: "Document filters" })).toBeInTheDocument();
    expect(screen.getAllByRole("tab")).toHaveLength(3);
    expect(screen.getByRole("tab", { name: "Documents" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("tab", { name: "Documents" })).toHaveAttribute("tabindex", "0");
    expect(screen.getByRole("tab", { name: "All" })).toHaveAttribute("aria-selected", "false");
    expect(screen.getByRole("tab", { name: "All" })).toHaveAttribute("tabindex", "-1");
    expect(screen.getByRole("tab", { name: "Reports" })).toHaveAttribute("tabindex", "-1");
  });

  it("ArrowLeft/ArrowRight move with wrapping; Home/End jump", () => {
    const onChange = jest.fn();
    render(<Tabs items={FILTERS} value="all" onChange={onChange} ariaLabel="Document filters" />);

    fireEvent.keyDown(screen.getByRole("tab", { name: "All" }), { key: "ArrowRight" });
    expect(onChange).toHaveBeenLastCalledWith("documents");

    onChange.mockClear();
    fireEvent.keyDown(screen.getByRole("tab", { name: "All" }), { key: "ArrowLeft" });
    expect(onChange).toHaveBeenLastCalledWith("reports");

    onChange.mockClear();
    fireEvent.keyDown(screen.getByRole("tab", { name: "All" }), { key: "End" });
    expect(onChange).toHaveBeenLastCalledWith("reports");

    onChange.mockClear();
    fireEvent.keyDown(screen.getByRole("tab", { name: "All" }), { key: "Home" });
    expect(onChange).toHaveBeenLastCalledWith("all");
  });

  it("click activates; Enter/Space on a focused tab activate; already-active Enter/Space is a no-op", () => {
    const onChange = jest.fn();
    render(<Tabs items={FILTERS} value="all" onChange={onChange} ariaLabel="Document filters" />);

    fireEvent.click(screen.getByRole("tab", { name: "Reports" }));
    expect(onChange).toHaveBeenLastCalledWith("reports");

    onChange.mockClear();
    fireEvent.keyDown(screen.getByRole("tab", { name: "Documents" }), { key: "Enter" });
    expect(onChange).toHaveBeenCalledWith("documents");

    onChange.mockClear();
    fireEvent.keyDown(screen.getByRole("tab", { name: "Documents" }), { key: " " });
    expect(onChange).toHaveBeenCalledWith("documents");

    onChange.mockClear();
    fireEvent.keyDown(screen.getByRole("tab", { name: "All" }), { key: "Enter" });
    fireEvent.keyDown(screen.getByRole("tab", { name: "All" }), { key: " " });
    expect(onChange).not.toHaveBeenCalled();
  });
});

// ---- V7-T04 DataTable -------------------------------------------------------

describe("V7-T04 DataTable", () => {
  it("renders the header row and one cell per column via render(); no empty/footer nodes without need", () => {
    render(
      <DataTable columns={RUN_COLUMNS} rows={RUN_ROWS} rowKey={(row) => row.id} empty={<p>No runs yet.</p>} />,
    );

    const table = screen.getByRole("table");
    expect(within(table).getByRole("columnheader", { name: "Title" })).toBeInTheDocument();
    expect(within(table).getByRole("columnheader", { name: "Cost" })).toBeInTheDocument();
    expect(within(table).getAllByRole("row")).toHaveLength(3); // header row + 2 data rows
    expect(within(table).getByRole("cell", { name: "First run" })).toBeInTheDocument();
    expect(within(table).getByRole("cell", { name: "Second run" })).toBeInTheDocument();
    expect(within(table).getByRole("cell", { name: "—" })).toBeInTheDocument();
    expect(screen.queryByText("No runs yet.")).not.toBeInTheDocument();
    expect(screen.queryByText("Showing 2 of 2 — the export carries 2 rows.")).not.toBeInTheDocument();
  });

  it("renders the empty node in place of the body rows when rows is empty", () => {
    render(
      <DataTable columns={RUN_COLUMNS} rows={[]} rowKey={(row: RunRow) => row.id} empty={<p>No runs yet.</p>} />,
    );

    const table = screen.getByRole("table");
    expect(within(table).getByText("No runs yet.")).toBeInTheDocument();
    expect(within(table).getByRole("columnheader", { name: "Title" })).toBeInTheDocument();
    expect(within(table).queryByRole("cell", { name: "First run" })).not.toBeInTheDocument();
  });

  it("footer slot renders below the table when provided", () => {
    render(
      <DataTable
        columns={RUN_COLUMNS}
        rows={RUN_ROWS}
        rowKey={(row) => row.id}
        empty={<p>No runs yet.</p>}
        footer={<span>Showing 2 of 2 — the export carries 2 rows.</span>}
      />,
    );

    const footer = screen.getByText("Showing 2 of 2 — the export carries 2 rows.");
    const table = screen.getByRole("table");
    expect(table.compareDocumentPosition(footer) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it("keys rows by rowKey: unique keys warn nothing; a duplicated rowKey output warns (probe)", () => {
    const errorSpy = jest.spyOn(console, "error").mockImplementation(() => {});
    const sameKeyWarnings = () =>
      errorSpy.mock.calls.filter((args) => String(args[0]).includes("same key")).length;
    try {
      const first = render(
        <DataTable columns={RUN_COLUMNS} rows={RUN_ROWS} rowKey={(row) => row.id} empty={null} />,
      );
      expect(sameKeyWarnings()).toBe(0);
      first.unmount();

      // Probe: if the component really keys by rowKey output, a constant rowKey must
      // trip React's duplicate-key warning. (An index-keyed table would stay silent.)
      render(<DataTable columns={RUN_COLUMNS} rows={RUN_ROWS} rowKey={() => "duplicate"} empty={null} />);
      expect(sameKeyWarnings()).toBeGreaterThan(0);
    } finally {
      errorSpy.mockRestore();
    }
  });
});

// ---- V7-T05 ui barrel exports ----------------------------------------------

describe("V7-T05 ui barrel exports", () => {
  it("exports the four primitives from @/components/ui", () => {
    expect(typeof uiBarrel.Switch).toBe("function");
    expect(typeof uiBarrel.RadioList).toBe("function");
    expect(typeof uiBarrel.Tabs).toBe("function");
    expect(typeof uiBarrel.DataTable).toBe("function");
  });

  it("exports the frozen prop types (compile-time proof via typed fixtures)", () => {
    const switchProps: SwitchProps = { checked: true, onChange: () => {}, label: "Night task", disabled: false, id: "night-task" };
    const radioProps: RadioListProps<Frequency> = { options: FREQUENCIES, value: null, onChange: () => {}, name: "Frequency" };
    const tabsProps: TabsProps<Filter> = { items: FILTERS, value: "all", onChange: () => {}, ariaLabel: "Filters" };
    const dataProps: DataTableProps<RunRow> = { columns: RUN_COLUMNS, rows: RUN_ROWS, rowKey: (row) => row.id, empty: null };

    expect([switchProps.checked, radioProps.value, tabsProps.value, typeof dataProps.rowKey]).toEqual([
      true,
      null,
      "all",
      "function",
    ]);
  });
});

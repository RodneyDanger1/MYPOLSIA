"use client";

import type { ReactNode } from "react";

/**
 * The table grammar copied verbatim from `finance/FinanceListPanel.tsx` (the shipped
 * finance lists): `w-full border-collapse text-left` table, a `border-b border-line`
 * header row of `ui-label` column headers, and `border-b border-line` body rows that
 * drop their last border. `columns[].render` produces each cell; `width` (optional)
 * lands as the header cell's `style` width.
 *
 * `rowKey(row)` is the React key for each row (probe-verified by the suite). When
 * `rows` is empty the `empty` node renders in a spanning cell in place of the body
 * rows (the header row stays); when `footer` is provided it renders below the table
 * in its own slot container.
 */
export type Column<R> = {
  key: string;
  header: string;
  render: (row: R) => ReactNode;
  width?: string;
};

export type DataTableProps<R> = {
  columns: Column<R>[];
  rows: R[];
  rowKey: (row: R) => string;
  empty: ReactNode;
  footer?: ReactNode;
  /** Accessible name for the table. Rendered as a visually-hidden `<caption>` so every
   *  DataTable surface is announced by what it lists, not as an unnamed "table". */
  label?: string;
};

export function DataTable<R>({ columns, rows, rowKey, empty, footer, label }: DataTableProps<R>) {
  return (
    <>
      <table className="w-full border-collapse text-left">
        {label === undefined ? null : <caption className="sr-only">{label}</caption>}
        <thead>
          <tr className="border-b border-line">
            {columns.map((column) => (
              <th
                key={column.key}
                scope="col"
                style={column.width !== undefined ? { width: column.width } : undefined}
                className="ui-label py-1 pr-3 font-normal text-ink-muted"
              >
                {column.header}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.length === 0 ? (
            <tr>
              <td colSpan={columns.length} className="py-2">
                {empty}
              </td>
            </tr>
          ) : (
            rows.map((row) => (
              <tr key={rowKey(row)} className="border-b border-line last:border-b-0">
                {columns.map((column) => (
                  <td key={column.key} className="py-1 pr-3 text-sm text-ink">
                    {column.render(row)}
                  </td>
                ))}
              </tr>
            ))
          )}
        </tbody>
      </table>
      {footer === undefined || footer === null ? null : (
        <div data-slot="footer" className="mt-2">
          {footer}
        </div>
      )}
    </>
  );
}

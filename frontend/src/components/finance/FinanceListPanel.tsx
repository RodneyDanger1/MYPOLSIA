"use client";

import { Card } from "@/components/ui";

import { ListSkeleton, ReadError } from "./states";
import { useFinanceRead } from "./useFinanceRead";

/**
 * One finance list surface: a real bounded read of a real endpoint, tabular rows only (no chart
 * can be drawn because no series endpoint exists), with its OWN three states so one broken list
 * cannot blank another region:
 *
 *   loading -> the skeleton ("not read yet" is not "empty")
 *   error   -> the failure named on the panel (ApiError.display), never an empty list standing in
 *   empty   -> the honest sentence, printed ONLY when the read succeeded and really returned nothing
 *
 * The count in the header is what THIS read measured — never a claimed total (the API serves none)
 * — and a full page says the read is capped (the Q-12 idiom), so the cap cannot masquerade as the
 * end of the list. A body that is not a list at all is named, not rendered.
 */

export type FinanceColumn<T> = {
  key: string;
  label: string;
  value: (row: T) => string;
};

type FinanceListPanelProps<T> = {
  /** Card heading, serif (the live panel voice). */
  title: string;
  /** Prefix for every data-testid this panel renders. */
  testId: string;
  /** The real endpoint, with its explicit bounded `limit`. */
  path: string;
  limit: number;
  /** Singular noun for the measured count ("30 snapshots"). */
  noun: string;
  /** Where a failure is named, e.g. "the revenue list". */
  errorLabel: string;
  emptyText: string;
  rowKey: (row: T) => string | number;
  columns: FinanceColumn<T>[];
  /** Bumped by the page's Refresh so every region re-reads together. */
  reloadKey: number;
};

export function FinanceListPanel<T>({
  title,
  testId,
  path,
  limit,
  noun,
  errorLabel,
  emptyText,
  rowKey,
  columns,
  reloadKey,
}: FinanceListPanelProps<T>) {
  const { data, error, loading } = useFinanceRead<T[]>(path, reloadKey);
  const rows = data !== null && Array.isArray(data) ? data : null;
  const brokenBody = data !== null && rows === null;
  // `>=` on purpose: a server that overshot its own cap must still be announced as capped.
  const truncated = rows !== null && rows.length >= limit;

  return (
    <Card
      title={title}
      data-testid={`finance-list-${testId}`}
      action={
        rows !== null ? (
          <span className="ui-label text-ink-muted" data-testid={`${testId}-count`}>
            {rows.length} {rows.length === 1 ? noun : `${noun}s`}
          </span>
        ) : undefined
      }
    >
      {loading ? <ListSkeleton testId={`${testId}-loading`} /> : null}

      {!loading && (error !== null || brokenBody) ? (
        <ReadError label={errorLabel} message={error ?? "the server returned no list of rows"} />
      ) : null}

      {!loading && error === null && !brokenBody && rows !== null && rows.length === 0 ? (
        <p className="text-sm text-ink-muted">{emptyText}</p>
      ) : null}

      {rows !== null && rows.length > 0 ? (
        <>
          <table className="w-full border-collapse text-left" data-testid={`${testId}-table`}>
            <thead>
              <tr className="border-b border-line">
                {columns.map((column) => (
                  <th
                    key={column.key}
                    scope="col"
                    className="ui-label py-1 pr-3 font-normal text-ink-muted"
                  >
                    {column.label}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={rowKey(row)} className="border-b border-line last:border-b-0">
                  {columns.map((column) => (
                    <td key={column.key} className="py-1 pr-3 text-sm text-ink">
                      {column.value(row)}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>

          {truncated ? (
            <p className="ui-label mt-2 text-ink-muted" data-testid={`${testId}-truncation`}>
              Showing the first {limit} — the API caps this read at {limit} per request.
            </p>
          ) : null}
        </>
      ) : null}
    </Card>
  );
}

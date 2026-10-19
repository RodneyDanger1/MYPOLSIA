"use client";

import { useEffect, useState } from "react";
import type { Dispatch, SetStateAction } from "react";

import { ApiError, api } from "@/lib/api";
import type { Invoice } from "@/lib/api";

/** The house error idiom (auto_mode/useAutoMode.ts :36-44): `ApiError` carries the named
 *  `.display` (code + message + how_to_fix); anything else shows its own message rather than a
 *  generic apology. The `typeof` guard keeps this hook alive under suite mocks that omit
 *  ApiError (`instanceof undefined` throws). */
export function errorText(e: unknown): string {
  if (typeof ApiError !== "undefined" && e instanceof ApiError) return e.display;
  if (e instanceof Error) return e.message;
  try {
    return JSON.stringify(e) ?? String(e);
  } catch {
    return String(e);
  }
}

export type InvoiceListRead = {
  /** The last successful body; `null` while nothing has been read yet OR the read failed — which
   *  of the two it is, `error` says. */
  rows: Invoice[] | null;
  error: string | null;
  loading: boolean;
  /** The server's FULL filtered count from THIS read's `X-Polsia-Total` (the same response as the
   *  rows). `null` when the response carried no readable header — the page then discloses only the
   *  cap it can see, never an invented count. Reset on every (re)read. */
  total: number | null;
  /** Exposed so an action's server answer can replace the row it names. The list endpoints and
   *  the create/send/void routes all answer `InvoiceResponse`, so the action answer IS the row —
   *  no follow-up read is invented for data the action already returned. */
  setRows: Dispatch<SetStateAction<Invoice[] | null>>;
};

/** One invoice-list read with the page's three states (the useFinanceRead/useAutoMode shape):
 *  `rows === null && error === null` IS "not read yet" — the page renders a skeleton for it,
 *  never an empty claim or a dash grid. `GET /invoices` answers a BARE ARRAY (`invoices.py:85`),
 *  so a 2xx body that is not an array is a broken read and is NAMED as one here. */
export function useInvoiceList(path: string): InvoiceListRead {
  const [rows, setRows] = useState<Invoice[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [total, setTotal] = useState<number | null>(null);

  useEffect(() => {
    let cancelled = false;
    setRows(null);
    setError(null);
    setTotal(null);
    api
      .get<unknown>(path, {
        onHeaders: (headers) => {
          if (cancelled) return;
          const raw = headers.get("X-Polsia-Total");
          if (raw === null) return;
          const parsed = Number(raw.trim());
          if (Number.isSafeInteger(parsed) && parsed >= 0) setTotal(parsed);
        },
      })
      .then((body) => {
        if (cancelled) return;
        if (!Array.isArray(body)) {
          setError("the server returned no invoice list");
          return;
        }
        setRows(body as Invoice[]);
      })
      .catch((e: unknown) => {
        if (!cancelled) setError(errorText(e));
      });
    return () => {
      cancelled = true;
    };
  }, [path]);

  return { rows, error, loading: rows === null && error === null, total, setRows };
}

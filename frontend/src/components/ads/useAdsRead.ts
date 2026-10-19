"use client";

import { useEffect, useState } from "react";

import { ApiError, api } from "@/lib/api";

/**
 * The ads page's read hook — the same three-state law the finance rebuild shipped, re-created in
 * this lane's territory with the same shape and the same words so no region can invent its own
 * loading/error dialect:
 *
 *   loading -> `data === null && error === null` (the consumer renders the aria-busy skeleton —
 *              "not read yet" is not "empty")
 *   error   -> the failure NAMED (`ApiError.display`; anything else shows its own message, never a
 *              generic apology)
 *   success -> the body; a 200 with a null/undefined body is a broken read and is NAMED as one
 *              here, because `data === null` with no error would render as an eternal skeleton —
 *              the same class of lie as a "no data" that was never read.
 *
 * A (re)read goes back through "not read yet" on purpose: the skeleton is the one honest way to say
 * "what you are looking at is being replaced" without faking progress. `reloadKey` re-issues the
 * read (Refresh / a changed filter path); nothing is polled or scheduled (audit C7).
 */
export type AdsRead<T> = {
  /** The last successful body; `null` while nothing has been read yet OR the read failed — which
   *  of the two it is, `error` says. */
  data: T | null;
  error: string | null;
  loading: boolean;
  /** The server's declared full count from THIS read's `X-Polsia-Total` header (the same header
   *  every bounded list route emits, `backend/app/api/v1/totals.py`). `null` when the response
   *  carried no readable total — the consumer then says only what the rows prove (the page
   *  length), never an invented count. Reset on every (re)read so a filtered read cannot inherit
   *  the unfiltered total. */
  total: number | null;
};

/** ApiError carries a named `.display` (code + message + fix hint); anything else shows its own
 *  message rather than a generic apology (the sibling pages' convention). */
export function errorText(e: unknown): string {
  if (e instanceof ApiError) return e.display;
  return e instanceof Error ? e.message : String(e);
}

export function useAdsRead<T>(path: string, reloadKey: number): AdsRead<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [total, setTotal] = useState<number | null>(null);

  useEffect(() => {
    let cancelled = false;
    setData(null);
    setError(null);
    setTotal(null);
    api
      .get<T>(path, {
        onHeaders: (headers) => {
          if (cancelled) return;
          const raw = headers.get("X-Polsia-Total");
          if (raw === null) return;
          const parsed = Number(raw.trim());
          // A header that is not a non-negative safe integer is not a count; leave it null so the
          // consumer prints only the page length it can see.
          if (Number.isSafeInteger(parsed) && parsed >= 0) setTotal(parsed);
        },
      })
      .then((body) => {
        if (cancelled) return;
        if (body === null || body === undefined) {
          setError("the server returned an empty body");
          return;
        }
        setData(body);
      })
      .catch((e: unknown) => {
        if (!cancelled) setError(errorText(e));
      });
    return () => {
      cancelled = true;
    };
  }, [path, reloadKey]);

  return { data, error, loading: data === null && error === null, total };
}

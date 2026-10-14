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

  useEffect(() => {
    let cancelled = false;
    setData(null);
    setError(null);
    api
      .get<T>(path)
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

  return { data, error, loading: data === null && error === null };
}

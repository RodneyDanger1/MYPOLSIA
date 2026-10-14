"use client";

import { useEffect, useState } from "react";

import { ApiError, api } from "@/lib/api";

/** ApiError carries a named `.display` (code + message + fix hint); anything else shows its own
 *  message rather than a generic apology (the sibling pages' convention). */
export function errorText(e: unknown): string {
  if (e instanceof ApiError) return e.display;
  return e instanceof Error ? e.message : String(e);
}

export type FinanceRead<T> = {
  /** The last successful body; `null` while nothing has been read yet OR the read failed — which
   *  of the two it is, `error` says. */
  data: T | null;
  error: string | null;
  loading: boolean;
};

/**
 * One finance read with the page's three states, kept in one place so the summary region and the
 * three list regions cannot drift into different vocabularies.
 *
 * `loading` is exactly the shape the Q-06 audit prescribes — `data === null && !error` — so
 * "not read yet" can never be confused with a value: while it is true the consumer renders a
 * skeleton, when it flips the consumer renders values (success) or the named failure (error).
 *
 * A (re)read goes back through "not read yet" on purpose: the skeleton is the one honest way to
 * say "what you are looking at is being replaced" without faking progress.
 *
 * A 2xx with an empty body is a broken read, and it is NAMED as one here — `data === null` with
 * no error would otherwise render as an eternal skeleton, which is the same class of lie as the
 * em-dash grid Q-06 removes. `reloadKey` re-issues the read (the Refresh affordance); nothing is
 * polled or scheduled (C7).
 */
export function useFinanceRead<T>(path: string, reloadKey: number): FinanceRead<T> {
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

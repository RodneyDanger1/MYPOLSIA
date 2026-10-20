"use client";

import { useEffect, useState } from "react";
import type { Dispatch, SetStateAction } from "react";

import { ApiError, api } from "@/lib/api";

import type { Prospect, ProspectList } from "./types";

/** The house error idiom (auto_mode/useAutoMode.ts :36-44; kept local so this hook survives suite
 *  mocks that omit ApiError): `ApiError.display` is the named refusal (code + message +
 *  how_to_fix); anything else shows its own message. */
export function errorText(e: unknown): string {
  if (typeof ApiError !== "undefined" && e instanceof ApiError) return e.display;
  if (e instanceof Error) return e.message;
  try {
    return JSON.stringify(e) ?? String(e);
  } catch {
    return String(e);
  }
}

export type ProspectListRead = {
  rows: Prospect[] | null;
  /** The server's full match count (`ProspectListResponse.total`), or null when the body did not
   *  carry a readable one — the page then states only what it measured (the page length). */
  total: number | null;
  error: string | null;
  loading: boolean;
  /** Exposed so an action's server answer can replace (or remove) the row it names — every write
   *  route returns the `ProspectResponse`, so no follow-up read is invented for it. */
  setRows: Dispatch<SetStateAction<Prospect[] | null>>;
  setTotal: Dispatch<SetStateAction<number | null>>;
};

/**
 * One prospects read with the page's three states: `rows === null && error === null` IS "not read
 * yet" (a skeleton, never an empty claim). A 2xx body without an `items` array is a broken read
 * and is NAMED as one, never laundered into "no prospects".
 *
 * `reloadKey` exists because the read path is the query itself: pressing Refresh with unchanged
 * filters must still re-read, and a changing effect dependency is the honest way to say that
 * (a cache-busting query parameter would have put an edit-only token on the wire).
 */
export function useProspectList(path: string, reloadKey = 0): ProspectListRead {
  const [rows, setRows] = useState<Prospect[] | null>(null);
  const [total, setTotal] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setRows(null);
    setTotal(null);
    setError(null);
    api
      .get<ProspectList>(path)
      .then((body) => {
        if (cancelled) return;
        const items = body !== null && typeof body === "object" ? body.items : undefined;
        if (!Array.isArray(items)) {
          setError("the endpoint answered without a prospect list");
          return;
        }
        setRows(items);
        const declared = body.total;
        setTotal(
          typeof declared === "number" && Number.isSafeInteger(declared) && declared >= 0
            ? declared
            : null,
        );
      })
      .catch((e: unknown) => {
        if (!cancelled) setError(errorText(e));
      });
    return () => {
      cancelled = true;
    };
  }, [path, reloadKey]);

  return { rows, total, error, loading: rows === null && error === null, setRows, setTotal };
}

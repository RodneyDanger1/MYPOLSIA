"use client";

import { useEffect, useState } from "react";
import type { Dispatch, SetStateAction } from "react";

import { ApiError, api } from "@/lib/api";
import type { Schedule } from "@/lib/api";

/** The house error idiom (auto_mode/useAutoMode.ts :36-44; kept local so this hook survives
 *  suite mocks that omit ApiError): `ApiError.display` is the named refusal (code + message +
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

export type ScheduleListRead = {
  rows: Schedule[] | null;
  error: string | null;
  loading: boolean;
  /** Exposed so an action's server answer can replace (or remove) the row it names — every
   *  schedules route returns the `ScheduleResponse`, so no follow-up read is invented. */
  setRows: Dispatch<SetStateAction<Schedule[] | null>>;
};

/** One schedules read with the page's three states: `rows === null && error === null` IS "not
 *  read yet" (a skeleton, never an empty claim). `GET /schedules` answers
 *  `{items, total}` (`schedules.py:39-47`); a 2xx body without an items array is a broken read
 *  and is NAMED as one, never laundered into "no schedules". */
export function useScheduleList(path: string): ScheduleListRead {
  const [rows, setRows] = useState<Schedule[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setRows(null);
    setError(null);
    api
      .get<unknown>(path)
      .then((body) => {
        if (cancelled) return;
        const items =
          body !== null && typeof body === "object"
            ? (body as { items?: unknown }).items
            : undefined;
        if (!Array.isArray(items)) {
          setError("the endpoint answered without a schedule list");
          return;
        }
        setRows(items as Schedule[]);
      })
      .catch((e: unknown) => {
        if (!cancelled) setError(errorText(e));
      });
    return () => {
      cancelled = true;
    };
  }, [path]);

  return { rows, error, loading: rows === null && error === null, setRows };
}

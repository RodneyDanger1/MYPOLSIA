"use client";

import { useEffect, useState } from "react";

import { api } from "@/lib/api";

import { isPhoneUnavailable, phoneErrorText, unwrapEnvelope } from "./phoneApi";

/**
 * One H-P1 read, as a four-state hook — "not read yet", "ok", "unavailable" (the store is not
 * mounted: `E_PHONE_UNAVAILABLE`), "failed" (anything else, named). The two honesty laws live
 * here so no consumer can re-implement them differently:
 *
 *   - a 200 that is NOT the `{ok, data}` envelope, or whose `data` fails validation, is a NAMED
 *     failure (`failed`, message from the validator) — never `undefined` rendered as empty;
 *   - `E_PHONE_UNAVAILABLE` is its own kind, because "the phone store is not here" is an honest
 *     state of the deployment, not an error page.
 *
 * `parse` must be a stable module-level function (it is a dependency) and `path` encodes every
 * read parameter (thread id, missed-only) — changing either refetches, which is exactly how the
 * thread view and the calls filter work.
 */

export type PhoneRead<T> =
  | { kind: "loading" }
  | { kind: "ok"; data: T }
  | { kind: "unavailable"; detail: string }
  | { kind: "failed"; detail: string };

export function usePhoneRead<T>(
  path: string,
  what: string,
  parse: (data: unknown) => T,
): PhoneRead<T> {
  const [state, setState] = useState<PhoneRead<T>>({ kind: "loading" });

  useEffect(() => {
    let cancelled = false;
    setState({ kind: "loading" });
    api
      .get<unknown>(path)
      .then((body) => {
        if (cancelled) return;
        let data: T;
        try {
          data = parse(unwrapEnvelope(what, body));
        } catch (e: unknown) {
          setState({ kind: "failed", detail: phoneErrorText(e) });
          return;
        }
        setState({ kind: "ok", data });
      })
      .catch((e: unknown) => {
        if (cancelled) return;
        setState(
          isPhoneUnavailable(e)
            ? { kind: "unavailable", detail: phoneErrorText(e) }
            : { kind: "failed", detail: phoneErrorText(e) },
        );
      });
    return () => {
      cancelled = true;
    };
  }, [path, what, parse]);

  return state;
}

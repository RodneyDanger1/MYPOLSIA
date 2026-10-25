"use client";

import { useEffect, useState } from "react";

import { readErrorText } from "@/components/dashboard/states";
import { api } from "@/lib/api";

/**
 * `GET /api/v1/config/phone` — the dashboard's "is a phone store mounted?" answer.
 *
 * CONTRACTS §4.16 (owner: the API seat): **200 in every deployment**, body
 * `{"configured": bool, "available": bool}`; `available` is false when the phone store is not
 * mounted. This is a CONFIG question, deliberately NOT under `/phone`: that module's contract is
 * "absent store = 503, there is no degraded mode" (`api/v1/phone.py`), so a card that asked
 * `/phone/summary` on a deployment that will never have a phone logged a refused request on every
 * dashboard load (F-37). It mirrors `GET /config/deepseek-key`, which answers `{configured:false}`
 * with 200 for the same reason.
 *
 * A FAILED or malformed config read is treated as "do not ask" and states its own reason: asking
 * `/phone/summary` on a guess is exactly the refused request this hook exists to prevent.
 */
export type PhoneAvailability = { configured: boolean; available: boolean };

export type PhoneAvailabilityRead =
  | { kind: "loading" }
  | { kind: "ok"; data: PhoneAvailability }
  | { kind: "failed"; detail: string };

export function usePhoneAvailability(): PhoneAvailabilityRead {
  const [state, setState] = useState<PhoneAvailabilityRead>({ kind: "loading" });

  useEffect(() => {
    let cancelled = false;
    api
      .get<PhoneAvailability>("/config/phone")
      .then((body) => {
        if (cancelled) return;
        // Both booleans are required. A body that carries neither is not an answer about
        // availability, and printing a guessed one would be a claim nothing measured.
        if (typeof body?.configured !== "boolean" || typeof body?.available !== "boolean") {
          setState({
            kind: "failed",
            detail: "the availability answer was not {configured, available}",
          });
          return;
        }
        setState({ kind: "ok", data: { configured: body.configured, available: body.available } });
      })
      .catch((e: unknown) => {
        if (!cancelled) setState({ kind: "failed", detail: readErrorText(e) });
      });
    return () => {
      cancelled = true;
    };
  }, []);

  return state;
}

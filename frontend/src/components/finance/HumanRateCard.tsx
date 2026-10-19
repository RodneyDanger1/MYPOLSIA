"use client";

import { useEffect, useRef, useState } from "react";

import { usdText } from "@/components/finance/format";
import { errorText } from "@/components/finance/useFinanceRead";
import { Card } from "@/components/ui";
import { api } from "@/lib/api";

/**
 * V23 / L3 — the Finance page's Human rate card.
 *
 * WHY (COMPLETENESS §7 item 11): the planning view's margin was permanently `—` because the
 * backend hard-coded `human_rate = None` and there was no surface to configure a rate. This card
 * is that surface: it reads `GET /config` -> `human_hourly_rate` and writes
 * `PUT /config {"human_hourly_rate": number | null}` (null is the Clear action).
 *
 * THE LAWS THIS CARD KEEPS:
 *  - the shown rate comes from the SERVER — `GET /config` on mount/refresh, and after a write the
 *    value is taken from the PUT RESPONSE. There is no optimistic state: while a write is in
 *    flight the old value stays on screen, and a refused write leaves it untouched;
 *  - absent (`null`) is stated in words ("Not set"), never rendered as 0 and never as `$0.00` —
 *    a measured 0 is a reading and renders `$0.00` like any other value;
 *  - a blank, non-numeric or negative draft is refused BY NAME inline with no PUT at all (`ge=0`
 *    is the server's bound too; refusing locally avoids a doomed round-trip);
 *  - a failed read is NAMED; the card draws no value and no controls it cannot trust. This card
 *    is an auxiliary read on a page whose one live ARIA alert region belongs to the summary, so
 *    its errors are the alert voice WITHOUT `role="alert"` (the page's own convention);
 *  - the copy claims exactly what the backend does: the rate feeds the planning margin template,
 *    and a margin also needs measured client capacity — a rate alone makes no margin.
 */

type ConfigWithRate = { human_hourly_rate?: unknown };

/** The rate the server actually served, or `null` when the body is not a trustworthy answer. */
function rateFromBody(body: unknown): { ok: true; rate: number | null } | { ok: false } {
  if (body === null || typeof body !== "object" || Array.isArray(body)) return { ok: false };
  const record = body as ConfigWithRate;
  // The post-0011 wire ALWAYS carries the key. An absent key (an older server, a truncated
  // read) is not "no rate was ever set" — it is an answer this card cannot read, so it must not
  // pretend either way.
  if (!("human_hourly_rate" in record)) return { ok: false };
  const raw = record.human_hourly_rate;
  if (raw === null || raw === undefined) return { ok: true, rate: null };
  if (typeof raw === "number" && Number.isFinite(raw) && raw >= 0) return { ok: true, rate: raw };
  return { ok: false };
}

export function HumanRateCard({ reloadKey }: { reloadKey: number }) {
  const [rate, setRate] = useState<number | null>(null);
  const [loading, setLoading] = useState(true);
  const [readError, setReadError] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const [pending, setPending] = useState<"save" | "clear" | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  /** V25/L5: the card owns its read, so it owns its retry — a failed `/config` read no longer
   *  waits for the page-level Refresh to come round. */
  const [attempt, setAttempt] = useState(0);

  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setReadError(null);
    api
      .get<unknown>("/config")
      .then((body) => {
        if (cancelled) return;
        const parsed = rateFromBody(body);
        if (!parsed.ok) {
          setReadError("the server's answer carried no readable human_hourly_rate");
          return;
        }
        setRate(parsed.rate);
        // A fresh authoritative read supersedes any stale write complaint.
        setActionError(null);
      })
      .catch((e: unknown) => {
        if (!cancelled) setReadError(errorText(e));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [reloadKey, attempt]);

  const write = async (next: number | null, kind: "save" | "clear") => {
    setPending(kind);
    setActionError(null);
    try {
      const body = await api.put<unknown>("/config", { human_hourly_rate: next });
      if (!mounted.current) return;
      const parsed = rateFromBody(body);
      if (!parsed.ok) {
        // A 2xx whose body we cannot read is NOT proof the write landed in the shape this card
        // shows. Keep the old value and name the broken answer.
        setActionError(
          "the server answered without a readable human_hourly_rate — the card was not updated",
        );
        return;
      }
      setRate(parsed.rate);
      setDraft("");
    } catch (e: unknown) {
      if (mounted.current) setActionError(errorText(e));
    } finally {
      if (mounted.current) setPending(null);
    }
  };

  const save = () => {
    if (pending !== null) return;
    const text = draft.trim();
    if (text === "") {
      setActionError("Enter a rate in USD per hour (0 or more), or use Clear rate.");
      return;
    }
    const parsed = Number(text);
    if (!Number.isFinite(parsed) || parsed < 0) {
      setActionError("The rate must be a number greater than or equal to 0.");
      return;
    }
    void write(parsed, "save");
  };

  return (
    <Card title="Human rate" data-testid="human-rate-card">
      {loading ? (
        <p className="ui-label text-ink-muted" data-testid="human-rate-loading" aria-busy="true">
          Loading…
        </p>
      ) : null}

      {!loading && readError !== null ? (
        <div className="text-sm text-alert" data-testid="human-rate-read-error">
          <p>Could not read the human hourly rate: {readError}</p>
          <button
            type="button"
            className="link ui-label mt-1"
            data-testid="human-rate-retry"
            onClick={() => setAttempt((n) => n + 1)}
          >
            Retry
          </button>
        </div>
      ) : null}

      {!loading && readError === null ? (
        <div className="space-y-3">
          {rate === null ? (
            <p className="text-sm text-ink-muted" data-testid="human-rate-absent">
              Not set — no human hourly rate has been saved. The planning margin stays
              unmeasurable, with its named reason, until one is.
            </p>
          ) : (
            <p className="text-sm text-ink" data-testid="human-rate-value">
              <span className="ui-label text-ink-muted">Current rate </span>
              <span className="font-bold">{usdText(rate)}</span> per hour
            </p>
          )}

          <p className="ui-label text-ink-muted">
            Used by the planning margin template. A margin needs this rate AND measured client
            capacity — the rate alone does not make one.
          </p>

          <div className="flex flex-wrap items-end gap-2">
            <div>
              <label
                className="ui-label block text-ink-muted"
                htmlFor="human-hourly-rate"
              >
                Human hourly rate (USD per hour)
              </label>
              <input
                id="human-hourly-rate"
                type="number"
                min={0}
                step="0.01"
                inputMode="decimal"
                autoComplete="off"
                className="mt-1 w-full border border-line bg-surface px-2 py-1 text-sm text-ink"
                value={draft}
                disabled={pending !== null}
                onChange={(event) => setDraft(event.target.value)}
              />
            </div>
            <button
              type="button"
              className="link ui-label"
              onClick={save}
              disabled={pending !== null}
            >
              {pending === "save" ? "Saving…" : "Save rate"}
            </button>
            {rate !== null ? (
              <button
                type="button"
                className="link ui-label"
                onClick={() => void write(null, "clear")}
                disabled={pending !== null}
              >
                {pending === "clear" ? "Clearing…" : "Clear rate"}
              </button>
            ) : null}
          </div>

          {actionError !== null ? (
            <p className="text-sm text-alert" data-testid="human-rate-error">
              {actionError}
            </p>
          ) : null}
        </div>
      ) : null}
    </Card>
  );
}

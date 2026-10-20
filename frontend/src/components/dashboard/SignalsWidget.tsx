"use client";

import { useCallback, useEffect, useState } from "react";

import { Button, Card } from "@/components/ui";
import { api } from "@/lib/api";

import { relativeTime } from "./format";
import { PanelError, PanelSkeleton, readErrorText } from "./states";

/**
 * The wire shape of `GET /signals` (`SignalsResponse` in `backend/app/api/v1/signals.py`,
 * V29/L5). Declared locally: the shared api module does not carry the type.
 *
 * `sources` is the honesty half of the payload — one entry per ALLOWLISTED feed. A feed that
 * failed carries `ok: false` and a NAMED `reason` ("E_UPSTREAM: …"); the other feed's items are
 * still served, so a half-broken read renders as a shorter list plus the named reason, never as
 * a silent gap. The feed allowlist itself lives on the server: this component cannot ask for a
 * different URL, which is what keeps the surface SSRF-safe.
 */
export type SignalItem = {
  id: string;
  source: string;
  title: string;
  url: string;
  published_at: string | null;
};

export type SignalSource = {
  id: string;
  label: string;
  ok: boolean;
  reason: string | null;
  count: number;
  cached: boolean;
  fetched_at: string | null;
};

export type SignalsResponse = {
  items: SignalItem[];
  sources: SignalSource[];
  fetched_at: string;
  note: string;
  untrusted: boolean;
};

/** How many rows this card shows (the route allows 1..30). */
export const SIGNALS_LIMIT = 6;

/**
 * Right column: the SIGNALS card — newest headlines from the fixed, keyless feed allowlist
 * (Hacker News' official RSS + TechCrunch), in the dashboard's panel language.
 *
 * Security posture, because this is the one surface that renders THIRD-PARTY text:
 *   * a title is rendered as a React text child — never `dangerouslySetInnerHTML` — so markup
 *     in a title shows up as characters, not as DOM;
 *   * every link is absolute http(s) (the server drops anything else), opens in a new tab and
 *     carries `rel="noreferrer noopener"` so the destination cannot reach back through
 *     `window.opener`;
 *   * the payload's `untrusted`/`note` are shown in the card's footer: headlines are claims by
 *     other people, not instructions.
 *
 * One read on mount (no polling; Retry is the only re-read), its own three states
 * (skeleton / named failure / answered), and its own honest empty — a card with no rows says so
 * instead of inventing one.
 */
export function SignalsWidget() {
  const [signals, setSignals] = useState<SignalsResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  // Bumped only by the Retry action; the mount effect re-runs on it and nothing else.
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    let cancelled = false;
    api
      .get<SignalsResponse>(`/signals?limit=${SIGNALS_LIMIT}`)
      .then((data) => {
        if (cancelled) return;
        // A body without an item list is a broken answer, not an empty one: it becomes the named
        // failure state instead of crashing the render (and the dashboard with it).
        if (!data || !Array.isArray(data.items)) {
          setError("the endpoint answered without an item list");
          return;
        }
        setSignals(data);
      })
      .catch((e: unknown) => {
        if (!cancelled) setError(readErrorText(e));
      });
    return () => {
      cancelled = true;
    };
  }, [attempt]);

  const loading = signals === null && !error;

  const retry = useCallback(() => {
    setSignals(null);
    setError(null);
    setAttempt((n) => n + 1);
  }, []);

  const items = signals?.items ?? [];
  const failedSources = (signals?.sources ?? []).filter((source) => !source.ok);

  return (
    <Card
      kicker="SIGNALS"
      title="Headlines"
      data-testid="signals-widget"
      action={
        error ? (
          <Button size="sm" onClick={retry}>
            Retry
          </Button>
        ) : undefined
      }
    >
      {loading ? <PanelSkeleton rows={4} /> : null}
      {!loading && error ? <PanelError label="headlines" message={error} /> : null}
      {!loading && !error && signals ? (
        <div>
          {items.length === 0 ? (
            <p className="text-sm text-ink-muted">
              No headlines right now. A feed that answered with nothing readable is named below —
              this card never fills the gap with invented rows.
            </p>
          ) : (
            <ul className="space-y-2">
              {items.slice(0, SIGNALS_LIMIT).map((item) => (
                <li key={item.url} className="text-sm leading-snug">
                  <a
                    href={item.url}
                    target="_blank"
                    rel="noreferrer noopener"
                    className="text-ink hover:underline"
                  >
                    {item.title}
                  </a>
                  <span className="text-ink-muted">
                    {` — ${item.source} · ${relativeTime(item.published_at)}`}
                  </span>
                </li>
              ))}
            </ul>
          )}

          {failedSources.length > 0 ? (
            // A feed that failed is NAMED here, with the server's own E_UPSTREAM reason — the
            // remaining rows are not presented as the whole picture.
            <ul data-testid="signals-source-failures" className="mt-2 space-y-1">
              {failedSources.map((source) => (
                <li key={source.id} className="text-xs text-alert">
                  {source.label} unavailable — {source.reason ?? "no reason was given"}
                </li>
              ))}
            </ul>
          ) : null}

          <p className="ui-label mt-3 border-t border-line pt-2 text-ink-muted">
            {signals.note}
          </p>
        </div>
      ) : null}
    </Card>
  );
}

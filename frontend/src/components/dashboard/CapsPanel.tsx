"use client";

import { useCallback, useEffect, useState } from "react";

import { Button, Card } from "@/components/ui";
import { api } from "@/lib/api";

import { absoluteStamp, NOT_MEASURED } from "./format";
import { PanelError, PanelSkeleton } from "./states";

/**
 * The wire shape of `GET /channels/caps` (`ChannelCapsResponse` in
 * `backend/app/api/v1/channels.py`, frozen in wave U3). `sent_today` is `int | null`: null
 * means the channel has NO local table that records sends — UNKNOWN, never zero (`email_logs`
 * counts email, `social_posts` counts twitter/linkedin; nobody records github, google_ads,
 * meta_ads, render or stripe sends). `cap` null means the send firewall configures no day cap
 * for the channel. `remaining` is `cap - sent_today` only when both sides are known, and it is
 * deliberately SIGNED — negative means the day already exceeds the cap. Declared locally: the
 * shared api module does not carry the type.
 */
type ChannelCapEntry = {
  channel: string;
  sent_today: number | null;
  cap: number | null;
  remaining: number | null;
};

type ChannelCaps = {
  channels: ChannelCapEntry[];
  generated_at: string | null;
};

/** The unmeasured reason, in the route's own words: the channel has no local send table to
 *  count — so the counter is unknown, and the panel says which fact is missing. */
const NO_COUNTER_TABLE = "not counted (no local send table)";

/**
 * Right column, bottom: the B9 "show the counter" panel — "SENT n of cap today" per channel,
 * read from the ONE surface that serves both the numerator and the denominator:
 * `GET /channels/caps` (the counters the policy firewall itself enforces against, plus the
 * firewall's own `RateCaps().per_channel_day` caps — one counting source, one truth).
 *
 * The null discipline is the point of this panel:
 *   * `sent_today: null` renders the not-measured marker WITH its reason — never a 0;
 *   * `cap: null` renders "no cap configured" — never an invented cap;
 *   * a negative `remaining` renders "over cap by N" — the value is served signed on purpose,
 *     and clamping it would hide a real overrun.
 *
 * One read on mount (no polling; the retry action is the only re-read), its own three states
 * (skeleton / named failure / answered), and its own honest empty for a truly empty channel
 * list. A failure here renders inside this card only — it cannot blank the dashboard.
 */
export function CapsPanel() {
  const [caps, setCaps] = useState<ChannelCaps | null>(null);
  const [error, setError] = useState<string | null>(null);
  // Bumped only by the Retry action; the mount effect re-runs on it and nothing else.
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    let cancelled = false;
    api
      .get<ChannelCaps>("/channels/caps")
      .then((data) => {
        if (cancelled) return;
        // A body without a channel list is a broken answer, not an empty one: it becomes the
        // named failure state instead of crashing the render (and the page with it).
        if (!data || !Array.isArray(data.channels)) {
          setError("the endpoint answered without a channel list");
          return;
        }
        setCaps(data);
      })
      .catch((e: unknown) => {
        if (!cancelled) setError(e instanceof Error ? e.message : String(e));
      });
    return () => {
      cancelled = true;
    };
  }, [attempt]);

  const loading = caps === null && !error;

  const retry = useCallback(() => {
    setCaps(null);
    setError(null);
    setAttempt((n) => n + 1);
  }, []);

  return (
    <Card
      kicker="Send caps today"
      title="Channels"
      action={
        error ? (
          <Button size="sm" onClick={retry}>
            Retry
          </Button>
        ) : undefined
      }
    >
      {loading ? <PanelSkeleton rows={4} /> : null}
      {!loading && error ? <PanelError label="channel send caps" message={error} /> : null}
      {!loading && !error && caps ? (
        <div>
          {caps.channels.length === 0 ? (
            <p className="text-sm text-ink-muted">
              The send policy names no channels, so there is nothing to count. Rows appear when
              the policy names a channel — this panel can only show the channels the policy
              enumerates.
            </p>
          ) : (
            <dl className="space-y-1">
              {caps.channels.map((entry) => (
                <div
                  key={entry.channel}
                  className="flex items-baseline justify-between gap-3"
                >
                  <dt className="ui-label text-ink-muted">{entry.channel}</dt>
                  <dd className="ui-label">
                    <Reading entry={entry} />
                  </dd>
                </div>
              ))}
            </dl>
          )}
          {/* Rows stay in the route's order (sorted by channel server-side; no client re-sort).
              The day is the company-local one (`channel_service.day_window`), NOT UTC — and the
              stamp is the snapshot instant `generated_at`, rendered in the browser's clock. */}
          <p className="ui-label mt-3 border-t border-line pt-2 text-ink-muted">
            As of {absoluteStamp(caps.generated_at)} · company-local day, reset at midnight in
            the company timezone
          </p>
        </div>
      ) : null}
    </Card>
  );
}

/**
 * One channel's reading — the whole null discipline in one function:
 *
 *   sent known + cap known  -> `sent / cap` + `k left` (or `over cap by k` when signed negative)
 *   sent unknown + cap      -> `— not counted (no local send table)` + `cap n`
 *   sent known + cap absent -> `sent sent` + `no cap configured`
 *   both unknown            -> both facts, neither as a number
 *
 * A `remaining` the wire leaves null while both sides are known is outside the frozen U3
 * contract (the route always computes it there); if it ever arrived, the row drops the
 * leftover clause instead of inventing one. The marker never renders alone — it always
 * carries its reason in the same text node.
 */
function Reading({ entry }: { entry: ChannelCapEntry }) {
  const { sent_today: sent, cap, remaining } = entry;

  if (sent !== null && cap !== null) {
    return (
      <>
        <span className="text-ink">
          {sent} / {cap}
        </span>
        {remaining !== null && remaining >= 0 ? (
          <span className="text-ink-muted"> · {remaining} left</span>
        ) : null}
        {remaining !== null && remaining < 0 ? (
          <span className="text-alert"> · over cap by {-remaining}</span>
        ) : null}
      </>
    );
  }

  if (sent === null && cap !== null) {
    return (
      <>
        <span className="text-ink-muted">
          {NOT_MEASURED} {NO_COUNTER_TABLE}
        </span>
        <span className="text-ink-muted"> · cap {cap}</span>
      </>
    );
  }

  if (sent !== null && cap === null) {
    return (
      <>
        <span className="text-ink">{sent} sent</span>
        <span className="text-ink-muted"> · no cap configured</span>
      </>
    );
  }

  return (
    <span className="text-ink-muted">
      {NOT_MEASURED} {NO_COUNTER_TABLE} · no cap configured
    </span>
  );
}

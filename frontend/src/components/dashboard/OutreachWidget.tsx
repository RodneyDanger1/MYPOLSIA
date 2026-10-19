"use client";

import { useEffect, useState } from "react";
import Link from "next/link";

import { Card, Chip } from "@/components/ui";
import { api } from "@/lib/api";

import { relativeTime } from "./format";
import { PanelError, PanelSkeleton, readErrorText } from "./states";

/** The wire row of `GET /outreach/prospects` (`ProspectResponse`); `status`/`created_at` are
 *  nullable server-side. Declared locally — the shared api module does not carry the type. */
type Prospect = {
  id: number;
  email: string;
  first_name: string | null;
  company: string | null;
  status: string | null;
  created_at: string | null;
};

/** Status text colours as a lookup with a fallback; the 800-step palette keeps every hue ≥ 4.5:1
 *  on the chip's light fill, and an unrecognised status renders neutral instead of borrowing
 *  another status's colour. */
const STATUS_TEXT: Record<string, string> = {
  new: "text-blue-800",
  contacted: "text-amber-800",
  replied: "text-green-800",
  converted: "text-green-800",
  unsubscribed: "text-ink-muted",
};

const WIDGET_LIMIT = 5;

/**
 * Right column, middle: the live `Email` widget's slot, adapted to the real surface this repo has —
 * `GET /outreach/prospects` (newest first).
 *
 * The live widget's `1 SENT · 0 RECEIVED` counters have no counterpart here: this backend runs no
 * mail transport, so no endpoint reports sent/received totals, and printing them would be
 * fabrication. The honest counter is over the rows actually loaded, and it says so.
 */
export function OutreachWidget() {
  const [prospects, setProspects] = useState<Prospect[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  // V24/L5: the failure's Retry re-issues this read.
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    let cancelled = false;
    setProspects(null);
    setError(null);
    api
      .get<Prospect[]>(`/outreach/prospects?limit=${WIDGET_LIMIT}`)
      .then((data) => {
        if (!cancelled) setProspects(data);
      })
      .catch((e: unknown) => {
        if (!cancelled) setError(readErrorText(e));
      });
    return () => {
      cancelled = true;
    };
  }, [attempt]);

  const loading = prospects === null && !error;

  return (
    <Card
      title="Outreach"
      action={
        <Link href="/outreach" className="ui-label text-link hover:underline">
          View all prospects
        </Link>
      }
    >
      {loading ? <PanelSkeleton rows={2} /> : null}
      {!loading && error ? (
        <PanelError
          label="prospects"
          message={error}
          onRetry={() => setAttempt((n) => n + 1)}
          retryTestId="outreach-widget-retry"
        />
      ) : null}
      {!loading && !error && prospects && prospects.length === 0 ? (
        <p className="text-sm text-ink-muted">
          No prospects yet. The outreach agent finds prospects when it runs — dispatch it from the
          Agents page, or open Outreach for the full list.
        </p>
      ) : null}
      {!loading && !error && prospects && prospects.length > 0 ? (
        <LatestProspect prospects={prospects} />
      ) : null}
    </Card>
  );
}

function LatestProspect({ prospects }: { prospects: Prospect[] }) {
  const latest = prospects[0];
  const statusText = STATUS_TEXT[latest.status ?? ""] ?? "text-ink-muted";
  const person = [latest.first_name, latest.company].filter((part) => Boolean(part)).join(" · ");

  return (
    <div>
      <p className="text-sm text-ink">{latest.email}</p>
      {person ? <p className="mt-0.5 text-sm text-ink-muted">{person}</p> : null}
      <div className="mt-2 flex flex-wrap items-center gap-2">
        {/* A null status is "not recorded" — the same em dash the outreach table uses. */}
        <Chip variant="soft" className={statusText}>
          {latest.status ?? "—"}
        </Chip>
        <span className="ui-label text-ink-muted">{relativeTime(latest.created_at)}</span>
      </div>
      <p className="ui-label mt-2 text-ink-muted">{prospects.length} most recent loaded</p>
    </div>
  );
}

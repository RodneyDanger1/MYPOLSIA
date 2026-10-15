"use client";

import Link from "next/link";

import { Card } from "@/components/ui";
import { parseSummary, type PhoneSummary } from "@/components/phone/phoneApi";
import { PhoneUnavailable } from "@/components/phone/PhoneStates";
import { usePhoneRead } from "@/components/phone/usePhoneRead";

import { PanelError, PanelSkeleton } from "./states";

/**
 * The left rail's `Phone` card (wave V16 / seat W3) — the dashboard's H-P1 consumer.
 *
 * Reads exactly ONE route, `GET /api/v1/phone/summary` (envelope `{ok, data}`), and renders what
 * the store really holds:
 *   - `unread_total` — printed only when it is a number; a null/absent value is the malformed-read
 *     state ("numeric unread_total"), NEVER "0 unread";
 *   - the newest 3 threads — each row: the resolved name, unread count when > 0, `last_when`
 *     verbatim or "time unknown" when the service stated none, and the last text. More than 3 is
 *     STATED ("Showing the 3 most recent threads of N"), never silently dropped;
 *   - `missed_calls_today` — every row the store reported today (masked number kept masked, the
 *     `when` string verbatim), capped at 3 with the remainder stated.
 *
 * Honest states, exactly like every other widget: not-read-yet is a skeleton; `E_PHONE_UNAVAILABLE`
 * (store not mounted) is the shared PHONE LINK UNAVAILABLE block — no rows, no counts; a
 * `store_status` answering anything but "ok" is the same block; any other failure is the panel
 * error naming the real message. Empty copy prints only after a successful, genuinely empty read.
 *
 * The whole card is read-only: this deployment's phone path is receive-only (the sending path is
 * an open owner offer in STANDING-ORDERS — not built, not pretended here).
 */

const THREADS_SHOWN = 3;
const MISSED_SHOWN = 3;

export function PhoneCard() {
  const summaryRead = usePhoneRead("/phone/summary", "summary", parseSummary);

  return (
    <Card title="Phone" aria-label="Phone">
      <div className="space-y-2">
        {summaryRead.kind === "loading" ? <PanelSkeleton rows={2} /> : null}

        {summaryRead.kind === "unavailable" ? (
          <PhoneUnavailable testId="phone-card-unavailable" detail={summaryRead.detail} />
        ) : null}

        {summaryRead.kind === "failed" ? (
          <PanelError label="the phone summary" message={summaryRead.detail} />
        ) : null}

        {summaryRead.kind === "ok" && summaryRead.data.store_status !== "ok" ? (
          // A 200 whose store_status is not "ok" is an ANSWER about the store, not data about
          // messages: the same honest block, and none of the (meaningless) arrays below render.
          <PhoneUnavailable
            testId="phone-card-unavailable"
            detail={`store_status: "${summaryRead.data.store_status}" — the phone store is not available in this deployment; nothing was read.`}
          />
        ) : null}

        {summaryRead.kind === "ok" && summaryRead.data.store_status === "ok" ? (
          <PhoneSummaryBody summary={summaryRead.data} />
        ) : null}
      </div>
      <Link href="/phone" className="link ui-label">
        OPEN PHONE
      </Link>
    </Card>
  );
}

function PhoneSummaryBody({ summary }: { summary: PhoneSummary }) {
  const threads = summary.threads.slice(0, THREADS_SHOWN);
  const missed = summary.missed_calls_today.slice(0, MISSED_SHOWN);

  return (
    <>
      <p data-testid="phone-unread-total" className="text-sm text-ink">
        <span className="font-bold">{summary.unread_total}</span> unread
      </p>

      <ul data-testid="phone-card-threads" className="space-y-1">
        {threads.map((thread) => (
          <li key={thread.thread_id} data-testid="phone-card-thread" className="text-sm">
            <span className="font-bold text-ink">{thread.with}</span>
            {thread.unread > 0 ? (
              <span className="ui-label ml-1 text-ink-muted">{thread.unread} unread</span>
            ) : null}
            <span data-testid="phone-card-thread-when" className="ui-label ml-2 text-ink-muted">
              {thread.last_when ?? "time unknown"}
            </span>
            <span className="block truncate text-ink-muted">{thread.last_text}</span>
          </li>
        ))}
      </ul>
      {summary.threads.length > THREADS_SHOWN ? (
        <p data-testid="phone-card-threads-more" className="ui-label text-ink-muted">
          Showing the {THREADS_SHOWN} most recent threads of {summary.threads.length}.
        </p>
      ) : null}
      {summary.threads.length === 0 ? (
        <p data-testid="phone-card-threads-empty" className="ui-label text-ink-muted">
          No threads in the phone store.
        </p>
      ) : null}

      <p className="ui-label border-t border-line pt-2 text-ink-muted">Missed calls today</p>
      <ul data-testid="phone-card-missed" className="space-y-1">
        {missed.map((call, index) => (
          <li
            key={`${call.who}-${call.when}-${index}`}
            data-testid="phone-card-missed-row"
            className="text-sm"
          >
            <span className="font-bold text-ink">{call.who}</span>
            <span className="ml-1 font-mono text-xs text-ink-muted">{call.num_masked}</span>
            <span className="ui-label ml-2 text-ink-muted">{call.when}</span>
          </li>
        ))}
      </ul>
      {summary.missed_calls_today.length > MISSED_SHOWN ? (
        <p data-testid="phone-card-missed-more" className="ui-label text-ink-muted">
          …and {summary.missed_calls_today.length - MISSED_SHOWN} more missed calls today.
        </p>
      ) : null}
      {summary.missed_calls_today.length === 0 ? (
        <p data-testid="phone-card-missed-empty" className="ui-label text-ink-muted">
          No missed calls today.
        </p>
      ) : null}
    </>
  );
}

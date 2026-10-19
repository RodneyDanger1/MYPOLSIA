"use client";

import Link from "next/link";
import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import { useSearchParams } from "next/navigation";

import { Button, SectionHeader } from "@/components/ui";
import { PanelError, PanelSkeleton } from "@/components/dashboard/states";
import { API_KEY, ApiError } from "@/lib/api";

import {
  CALLS_WINDOW,
  MESSAGES_WINDOW,
  SEND_BODY_MAX,
  isSendBlocked,
  isPhoneUnavailable,
  parseCalls,
  parseMessages,
  parseRefusalBody,
  parseSummary,
  parseThreadParam,
  phoneErrorText,
  sendBlockedReason,
  sendRefusalCode,
  sendSms,
  sentClock,
  type PhoneMessage,
  type PhoneSendResult,
} from "./phoneApi";
import { PhoneUnavailable } from "./PhoneStates";
import { PhoneCodesPanel } from "./PhoneCodesPanel";
import { PhoneMediaSection } from "./PhoneMedia";
import { usePhoneRead } from "./usePhoneRead";

/**
 * The `/phone` console (wave V16 / seat W3) — every read is a REAL H-P1 route, every state honest:
 *
 *   GET /api/v1/phone/summary                          -> threads index + the page's gate state
 *   GET /api/v1/phone/messages?limit&thread_id         -> recent window, or one thread's window
 *   GET /api/v1/phone/calls?limit&missed_only          -> calls list, filterable
 *   GET /api/v1/phone/media?thread_id=N                -> per-thread media grid (V18)
 *   GET /api/v1/phone/media/{part_id}                  -> one streamed part (viewer/download)
 *   GET /api/v1/phone/codes?limit&within_hours         -> the owner-requested codes panel (V19;
 *                                                         fetch-on-click ONLY — see PhoneCodesPanel)
 *   GET /api/v1/phone/export                           -> streaming ZIP download (below)
 *
 * What is deliberately NOT here: no poll loop (the store is read-only in this deployment and the
 * page says so); search is CLIENT-side over the one loaded window, and the page states that window
 * ("search filters what was loaded") instead of pretending to search the whole store — the H-P1
 * messages route serves one window and nothing more. `?thread=N` follows the tasks-page deep-link
 * law: digits only; a junk param is the documented no-op.
 *
 * SENDING (wave V17): the composer is the REAL `POST /api/v1/phone/send`. It never optimistically
 * renders a send: the confirmation appears only after the server answered with the driver-observed
 * `sent_at` and a masked recipient. A sandbox/unpaired/unavailable refusal disables the composer
 * and states the NAMED reason (plus a "Try again" that re-attempts, because the deployment can be
 * fixed while the page is open); a validation refusal renders inline and leaves the form usable.
 *
 * The page gate: the summary read decides whether ANY data surface renders. Unavailable
 * (`E_PHONE_UNAVAILABLE`, or store_status != "ok") renders the ONE shared PHONE LINK UNAVAILABLE
 * block; a failed summary read renders the named panel error; only an ok summary mounts the
 * threads/messages/calls sections (their own reads keep their own four states).
 *
 * The download is the V15 blob pattern (`SitePackageSection.tsx`, wave V15/W2 item 4-07): a raw
 * `fetch` — `lib/api.ts` is JSON-only by construction — auth from the client's single credential
 * source (`API_KEY`, sent only when non-empty), saved under the SERVER's own
 * `Content-Disposition` filename (a stated `phone-export.zip` fallback if the header is
 * unreadable — the bytes are still the server's). Refusals render by name with the server's fix
 * hint; transport failures name themselves; never a silent no-op.
 */

const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost";

/** The refusal carried by a response body is parsed by `phoneApi.parseRefusalBody` — the SAME
 *  parser the media stream uses (one refusal vocabulary for every raw-fetch surface). */

/** The server's own save name out of `Content-Disposition` (quoted and unquoted forms). An
 *  unrecognisable header falls back to `phone-export.zip` — a stated fallback name, never an
 *  invented one; the CONTENT is always exactly the server's bytes. */
function filenameFromDisposition(header: string | null): string {
  if (header) {
    const match = /filename\s*=\s*"?([^";]+)"?/i.exec(header);
    if (match && match[1].trim() !== "") return match[1].trim();
  }
  return "phone-export.zip";
}

/** The duration cell: the measured seconds, or the em dash when the store stated none (a missed
 *  call has no duration) — never a fabricated 0. */
function durationText(durS: number | null | undefined): string {
  return typeof durS === "number" ? `${durS}s` : "—";
}

export function PhoneConsole() {
  const searchParams = useSearchParams();
  const rawThread = searchParams ? searchParams.get("thread") : null;
  const threadId = parseThreadParam(rawThread);

  const summaryRead = usePhoneRead("/phone/summary", "summary", parseSummary);
  const messagesRead = usePhoneRead(
    threadId === null
      ? `/phone/messages?limit=${MESSAGES_WINDOW}`
      : `/phone/messages?limit=${MESSAGES_WINDOW}&thread_id=${threadId}`,
    "messages",
    parseMessages,
  );
  const [missedOnly, setMissedOnly] = useState(false);
  const callsRead = usePhoneRead(
    `/phone/calls?limit=${CALLS_WINDOW}${missedOnly ? "&missed_only=true" : ""}`,
    "calls",
    parseCalls,
  );

  const [query, setQuery] = useState("");

  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  // ── the send composer (wave V17) ───────────────────────────────────────────────────────────
  // `to` prefills from the page's own deep link; the rest is deliberately plain state. The result
  // is cleared on every edit (a stale "sent" next to new text would be a lie by layout).
  const [sendTo, setSendTo] = useState(threadId === null ? "" : String(threadId));
  const [sendBody, setSendBody] = useState("");
  const [sending, setSending] = useState(false);
  const [sendError, setSendError] = useState<string | null>(null);
  const [sendBlocked, setSendBlocked] = useState<string | null>(null);
  const [sendResult, setSendResult] = useState<PhoneSendResult | null>(null);
  const [sendAttempts, setSendAttempts] = useState(0);

  useEffect(() => {
    setSendTo(threadId === null ? "" : String(threadId));
    setSendResult(null);
  }, [threadId]);

  const canSend =
    sendTo.trim() !== "" && sendBody.trim() !== "" && !sending && sendBlocked === null;

  const submitSend = useCallback(
    async (event: FormEvent<HTMLFormElement>) => {
      event.preventDefault();
      if (!canSend) return;
      setSending(true);
      setSendError(null);
      setSendResult(null);
      setSendAttempts((count) => count + 1);
      try {
        const result = await sendSms(sendTo.trim(), sendBody);
        if (!mounted.current) return;
        // The server's own observation, rendered as-is: `sent_at` clock + masked recipient.
        setSendResult(result);
        setSendBody("");
      } catch (e: unknown) {
        if (!mounted.current) return;
        setSendError(phoneErrorText(e));
        if (isSendBlocked(e)) setSendBlocked(sendBlockedReason(sendRefusalCode(e)));
      } finally {
        if (mounted.current) setSending(false);
      }
    },
    [canSend, sendBody, sendTo],
  );

  const [downloading, setDownloading] = useState(false);
  const [downloadError, setDownloadError] = useState<string | null>(null);
  const [downloadDone, setDownloadDone] = useState<string | null>(null);

  const download = useCallback(async () => {
    setDownloading(true);
    setDownloadError(null);
    setDownloadDone(null);
    try {
      const res = await fetch(`${API_URL}/api/v1/phone/export`, {
        headers: { ...(API_KEY ? { "X-API-Key": API_KEY } : {}) },
      });
      if (!res.ok) {
        // Read the refusal BEFORE building the error, so a coded body (E_PHONE_UNAVAILABLE, …)
        // keeps its code + fix hint instead of degrading to a bare HTTP status.
        const text = await res.text().catch(() => res.statusText);
        let parsed: unknown;
        try {
          parsed = JSON.parse(text);
        } catch {
          parsed = undefined;
        }
        const refusal = parseRefusalBody(parsed);
        throw refusal
          ? new ApiError(res.status, refusal.message, refusal.code, refusal.howToFix, parsed)
          : new ApiError(res.status, text || res.statusText, `E_HTTP_${res.status}`);
      }
      const blob = await res.blob();
      const filename = filenameFromDisposition(res.headers.get("Content-Disposition"));
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = filename;
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
      URL.revokeObjectURL(url);
      if (mounted.current) setDownloadDone(`Downloaded ${filename}.`);
    } catch (e: unknown) {
      if (mounted.current) setDownloadError(phoneErrorText(e));
    } finally {
      if (mounted.current) setDownloading(false);
    }
  }, []);

  const threadName =
    threadId !== null && summaryRead.kind === "ok"
      ? (summaryRead.data.threads.find((thread) => thread.thread_id === threadId)?.with ?? null)
      : null;

  const loaded: PhoneMessage[] = messagesRead.kind === "ok" ? messagesRead.data : [];
  const trimmedQuery = query.trim().toLowerCase();
  const filtered =
    trimmedQuery === ""
      ? loaded
      : loaded.filter(
          (message) =>
            message.text.toLowerCase().includes(trimmedQuery) ||
            message.who.toLowerCase().includes(trimmedQuery),
        );

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-end justify-between gap-2 border-b border-line pb-2">
        <div>
          <h1 className="text-3xl font-bold text-ink">Phone</h1>
          <p className="text-sm text-ink-muted">
            The phone-link store, read exactly as it answers — plus a send composer that only
            claims a send the paired Google Messages page confirmed.
          </p>
        </div>
        <Button data-testid="phone-download" onClick={download} disabled={downloading}>
          {downloading ? "Downloading…" : "Download export"}
        </Button>
      </div>

      {downloadError !== null ? (
        <div
          role="alert"
          data-testid="phone-download-error"
          className="border border-alert bg-surface p-3"
        >
          <p className="ui-label text-alert">Download refused</p>
          <p className="mt-1 text-sm text-ink">{downloadError}</p>
        </div>
      ) : null}

      {downloadDone !== null ? (
        <p role="status" data-testid="phone-download-done" className="ui-label text-ink-muted">
          {downloadDone}
        </p>
      ) : null}

      {summaryRead.kind === "loading" ? <PanelSkeleton rows={3} /> : null}

      {summaryRead.kind === "unavailable" ? (
        <PhoneUnavailable testId="phone-unavailable" detail={summaryRead.detail} />
      ) : null}

      {summaryRead.kind === "failed" ? (
        <PanelError label="the phone summary" message={summaryRead.detail} />
      ) : null}

      {summaryRead.kind === "ok" && summaryRead.data.store_status !== "ok" ? (
        <PhoneUnavailable
          testId="phone-unavailable"
          detail={`store_status: "${summaryRead.data.store_status}" — the phone store is not available in this deployment; nothing was read.`}
        />
      ) : null}

      {summaryRead.kind === "ok" && summaryRead.data.store_status === "ok" ? (
        <>

          <section aria-label="Send a message" data-testid="phone-composer" className="space-y-2">
            <SectionHeader kicker="SEND" title="Send a message" />
            <p data-testid="phone-send-note" className="ui-label text-ink-muted">
              One SMS through the paired Google Messages web session. The confirmation names the
              masked recipient and the server&apos;s own send time — nothing is claimed until the
              page confirms it.
            </p>
            <form onSubmit={submitSend} className="space-y-2">
              <label className="block text-sm text-ink">
                To
                <input
                  type="text"
                  data-testid="phone-send-to"
                  aria-label="Send to: a thread id or E.164 digits"
                  aria-invalid={sendError !== null ? true : undefined}
                  aria-describedby={sendError !== null ? "phone-send-error" : undefined}
                  placeholder="thread id or +155****4477"
                  value={sendTo}
                  onChange={(event) => {
                    setSendTo(event.target.value);
                    setSendResult(null);
                  }}
                  className="mt-1 w-full border border-line bg-surface px-2 py-1 font-mono text-sm text-ink"
                />
              </label>
              <label className="block text-sm text-ink">
                Message
                <textarea
                  data-testid="phone-send-body"
                  aria-label="Message body"
                  aria-invalid={sendError !== null ? true : undefined}
                  aria-describedby={sendError !== null ? "phone-send-error" : undefined}
                  rows={3}
                  maxLength={SEND_BODY_MAX}
                  value={sendBody}
                  onChange={(event) => {
                    setSendBody(event.target.value);
                    setSendResult(null);
                  }}
                  className="mt-1 w-full border border-line bg-surface px-2 py-1 text-sm text-ink"
                />
              </label>
              <div className="flex flex-wrap items-center gap-2">
                <Button type="submit" data-testid="phone-send-submit" disabled={!canSend}>
                  {sending ? "Sending…" : "Send"}
                </Button>
                <span className="ui-label text-ink-muted">
                  {sendBody.length}/{SEND_BODY_MAX}
                </span>
              </div>
            </form>

            {sendBlocked !== null ? (
              <div
                role="status"
                data-testid="phone-send-blocked"
                className="border border-line bg-surface p-3"
              >
                <p className="text-sm text-ink">{sendBlocked}</p>
                <p className="ui-label mt-1 text-ink-muted">
                  The composer stays disabled until the deployment changes. Fixed it? Retry.
                </p>
                <Button
                  type="button"
                  data-testid="phone-send-retry"
                  onClick={() => {
                    setSendBlocked(null);
                    setSendError(null);
                  }}
                >
                  Try again
                </Button>
              </div>
            ) : null}

            {sendError !== null ? (
              <div
                key={`phone-send-error-${sendAttempts}`}
                id="phone-send-error"
                role="alert"
                data-testid="phone-send-error"
                className="border border-alert bg-surface p-3"
              >
                <p className="ui-label text-alert">Send refused</p>
                <p className="mt-1 text-sm text-ink">{sendError}</p>
              </div>
            ) : null}

            {sendResult !== null ? (
              <p role="status" data-testid="phone-send-result" className="text-sm text-ink">
                Sent at {sentClock(sendResult.sent_at)} to {sendResult.to_masked} — confirmed by the
                page.
              </p>
            ) : null}
          </section>

          <section aria-label="Phone threads" className="space-y-2">
            <SectionHeader kicker="THREADS" title="Threads" />
            {summaryRead.data.threads.length === 0 ? (
              <p data-testid="phone-threads-empty" className="ui-label text-ink-muted">
                No threads in the phone store.
              </p>
            ) : (
              <ul data-testid="phone-threads" className="space-y-1">
                {summaryRead.data.threads.map((thread) => (
                  <li key={thread.thread_id} data-testid="phone-thread-row" className="text-sm">
                    <Link href={`/phone?thread=${thread.thread_id}`} className="link font-bold">
                      {thread.with}
                    </Link>
                    {thread.unread > 0 ? (
                      <span className="ui-label ml-1 text-ink-muted">{thread.unread} unread</span>
                    ) : null}
                    <span className="ui-label ml-2 text-ink-muted">
                      {thread.last_when ?? "time unknown"}
                    </span>
                    <span className="block truncate text-ink-muted">{thread.last_text}</span>
                  </li>
                ))}
              </ul>
            )}
          </section>

          <section aria-label="Phone messages" className="space-y-2">
            <SectionHeader
              kicker="MESSAGES"
              title={
                threadId === null
                  ? "Recent messages"
                  : threadName !== null
                    ? `Thread #${threadId} — ${threadName}`
                    : `Thread #${threadId}`
              }
              action={
                threadId !== null ? (
                  <Link href="/phone" data-testid="phone-thread-back" className="link ui-label">
                    ← ALL THREADS
                  </Link>
                ) : null
              }
            />
            <p data-testid="phone-messages-window" className="ui-label text-ink-muted">
              {threadId === null
                ? `Reading up to the newest ${MESSAGES_WINDOW} messages — one window; search filters what was loaded.`
                : `Reading up to the newest ${MESSAGES_WINDOW} messages of this thread — one window; search filters what was loaded.`}
            </p>
            <input
              type="search"
              data-testid="phone-search"
              aria-label="Search loaded messages"
              placeholder="Search loaded messages…"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              className="w-full border border-line bg-surface px-2 py-1 text-sm text-ink"
            />

            {messagesRead.kind === "loading" ? <PanelSkeleton rows={3} /> : null}
            {messagesRead.kind === "unavailable" ? (
              <PhoneUnavailable testId="phone-messages-unavailable" detail={messagesRead.detail} />
            ) : null}
            {messagesRead.kind === "failed" ? (
              <PanelError label="phone messages" message={messagesRead.detail} />
            ) : null}

            {messagesRead.kind === "ok" && trimmedQuery !== "" && filtered.length > 0 ? (
              <p role="status" data-testid="phone-search-note" className="ui-label text-ink-muted">
                Filtering the loaded window: {filtered.length} of {loaded.length} messages match “
                {query}”.
              </p>
            ) : null}
            {messagesRead.kind === "ok" && trimmedQuery !== "" && filtered.length === 0 ? (
              <p role="status" data-testid="phone-search-empty" className="ui-label text-ink-muted">
                No loaded messages match “{query}”.
              </p>
            ) : null}
            {messagesRead.kind === "ok" && trimmedQuery === "" && filtered.length === 0 ? (
              <p data-testid="phone-messages-empty" className="ui-label text-ink-muted">
                {threadId === null ? "No messages in the phone store." : "No messages in this thread."}
              </p>
            ) : null}

            {messagesRead.kind === "ok" && filtered.length > 0 ? (
              <ul data-testid="phone-messages" className="space-y-1">
                {filtered.map((message, index) => (
                  <li
                    key={`${message.thread_id}-${message.when}-${index}`}
                    data-testid="phone-message-row"
                    className="text-sm"
                  >
                    <span className="ui-label text-ink-muted">{message.when}</span>
                    <span className="ui-label ml-2 text-ink-muted">{message.dir}</span>
                    <span className="ml-2 font-bold text-ink">{message.who}</span>
                    {threadId === null ? (
                      <Link
                        href={`/phone?thread=${message.thread_id}`}
                        className="link ui-label ml-2"
                      >
                        Thread #{message.thread_id}
                      </Link>
                    ) : null}
                    <span className="block text-ink">{message.text}</span>
                  </li>
                ))}
              </ul>
            ) : null}
          </section>

          <PhoneMediaSection threadId={threadId} />

          <section aria-label="Phone calls" className="space-y-2">
            <SectionHeader
              kicker="CALLS"
              title="Calls"
              action={
                <label className="ui-label flex items-center gap-1 text-ink">
                  <input
                    type="checkbox"
                    data-testid="phone-calls-missed-only"
                    checked={missedOnly}
                    onChange={(event) => setMissedOnly(event.target.checked)}
                  />
                  Missed only
                </label>
              }
            />
            {callsRead.kind === "loading" ? <PanelSkeleton rows={2} /> : null}
            {callsRead.kind === "unavailable" ? (
              <PhoneUnavailable testId="phone-calls-unavailable" detail={callsRead.detail} />
            ) : null}
            {callsRead.kind === "failed" ? (
              <PanelError label="phone calls" message={callsRead.detail} />
            ) : null}
            {callsRead.kind === "ok" && callsRead.data.length === 0 ? (
              <p data-testid="phone-calls-empty" className="ui-label text-ink-muted">
                {missedOnly ? "No missed calls in the loaded window." : "No calls in the loaded window."}
              </p>
            ) : null}
            {callsRead.kind === "ok" && callsRead.data.length > 0 ? (
              <ul data-testid="phone-calls" className="space-y-1">
                {callsRead.data.map((call, index) => (
                  <li
                    key={`${call.when}-${index}`}
                    data-testid="phone-call-row"
                    className="text-sm"
                  >
                    <span className="ui-label text-ink-muted">{call.type}</span>
                    <span className="ml-2 font-bold text-ink">{call.who}</span>
                    <span className="ml-2 font-mono text-xs text-ink-muted">{call.num_masked}</span>
                    <span className="ui-label ml-2 text-ink-muted">{call.when}</span>
                    <span className="ml-2 text-ink">{durationText(call.dur_s)}</span>
                  </li>
                ))}
              </ul>
            ) : null}
          </section>

          <PhoneCodesPanel />
        </>
      ) : null}
    </div>
  );
}

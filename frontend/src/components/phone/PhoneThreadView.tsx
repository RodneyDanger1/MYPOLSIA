"use client";

import { useEffect, useState } from "react";

import { PanelSkeleton } from "@/components/dashboard/states";

import {
  MESSAGES_WINDOW,
  fetchMessages,
  isPhoneUnavailable,
  messageWhenText,
  phoneErrorText,
  type PhoneMessage,
} from "./phoneApi";
import { PhoneUnavailable } from "./PhoneStates";

/**
 * The `/phone` THREAD CONVERSATION panel (wave V22 / seat L2) — the view the data already had.
 *
 * `GET /phone/messages?thread_id=…` has served one thread's newest window since H-P1, but the page
 * only exposed the list (and a `?thread=` route change). This panel is the in-page conversation:
 * it mounts only when the owner SELECTS a thread, so the route is never read on page mount. Every
 * read is the real one (`fetchMessages` → `api.get` → the shared envelope + `parseMessages` laws);
 * there is no mock, no sample row and no client-side re-sort — the store's newest-first order and
 * its own `when` wording render verbatim (`null` → the app-wide "time unknown").
 *
 * Four honest states, borrowed from the page's own vocabulary:
 *   - loading: the page's skeleton, `aria-busy` — "not read yet" is never "empty";
 *   - ok, zero rows: "No messages in this thread." — printed only after a successful empty read;
 *   - failed: the refusal BY NAME (code + message + fix hint, `ApiError.display`) inline, with a
 *     retry that performs a real second read;
 *   - unavailable: `E_PHONE_UNAVAILABLE` reuses the page's ONE `PhoneUnavailable` 503 block.
 *
 * Privacy, the same laws the codes panel holds: the rows live in component state only — no
 * storage, no console — and the effect's cleanup marks the panel dead, so closing it (or
 * unmounting the page) drops every held row and a late answer can never render.
 */

type ThreadRead =
  | { kind: "loading" }
  | { kind: "ok"; rows: PhoneMessage[] }
  | { kind: "unavailable"; detail: string }
  | { kind: "failed"; detail: string };

export function PhoneThreadView({
  threadId,
  threadName,
  onClose,
}: {
  threadId: number;
  threadName: string;
  onClose: () => void;
}) {
  const [attempt, setAttempt] = useState(0);
  const [state, setState] = useState<ThreadRead>({ kind: "loading" });

  useEffect(() => {
    let alive = true;
    setState({ kind: "loading" });
    fetchMessages(threadId, MESSAGES_WINDOW)
      .then((rows) => {
        if (alive) setState({ kind: "ok", rows });
      })
      .catch((e: unknown) => {
        if (!alive) return;
        setState(
          isPhoneUnavailable(e)
            ? { kind: "unavailable", detail: phoneErrorText(e) }
            : { kind: "failed", detail: phoneErrorText(e) },
        );
      });
    return () => {
      // Clear on unmount/close: a late answer is ignored, and the rows die with the component.
      alive = false;
    };
  }, [threadId, attempt]);

  return (
    <section
      aria-label={`Conversation with ${threadName}`}
      data-testid="phone-thread-view"
      className="space-y-2 border border-line bg-surface p-3"
    >
      <div className="flex flex-wrap items-baseline justify-between gap-2 border-b border-line pb-2">
        <div>
          <p className="ui-label text-ink-muted">CONVERSATION</p>
          <h3 data-testid="phone-thread-heading" className="text-lg font-bold text-ink">
            Thread #{threadId} — {threadName}
          </h3>
        </div>
        <button type="button" data-testid="phone-thread-close" onClick={onClose} className="link ui-label">
          ← All threads
        </button>
      </div>

      {state.kind === "loading" ? (
        <div data-testid="phone-thread-loading" aria-busy="true" className="space-y-2">
          <p role="status" className="ui-label text-ink-muted">
            Reading this thread…
          </p>
          <PanelSkeleton rows={4} />
        </div>
      ) : null}

      {state.kind === "unavailable" ? (
        <PhoneUnavailable testId="phone-thread-unavailable" detail={state.detail} />
      ) : null}

      {state.kind === "failed" ? (
        <div role="alert" data-testid="phone-thread-error" className="border border-alert bg-surface p-3">
          <p className="ui-label text-alert">Could not read this thread</p>
          <p className="mt-1 text-sm text-ink">{state.detail}</p>
          <p className="ui-label mt-1 text-ink-muted">
            Nothing was read for this conversation; the thread list is unchanged.
          </p>
          <button
            type="button"
            data-testid="phone-thread-retry"
            onClick={() => setAttempt((count) => count + 1)}
            className="link ui-label mt-1"
          >
            Try again
          </button>
        </div>
      ) : null}

      {state.kind === "ok" && state.rows.length === 0 ? (
        <p data-testid="phone-thread-empty" className="ui-label text-ink-muted">
          No messages in this thread.
        </p>
      ) : null}

      {state.kind === "ok" && state.rows.length > 0 ? (
        <>
          <p data-testid="phone-thread-window" className="ui-label text-ink-muted">
            Showing the newest {state.rows.length} message{state.rows.length === 1 ? "" : "s"} of this
            thread — one window of up to {MESSAGES_WINDOW} is read per open.
          </p>
          <ul data-testid="phone-thread-messages" className="space-y-1">
            {state.rows.map((message, index) => (
              <li
                key={`${message.thread_id}-${index}`}
                data-testid="phone-thread-message"
                className="text-sm"
              >
                <span className="ui-label text-ink-muted">{messageWhenText(message.when)}</span>
                <span className="ui-label ml-2 text-ink-muted">{message.dir}</span>
                <span className="ml-2 font-bold text-ink">{message.who}</span>
                <span className="block text-ink">{message.text}</span>
              </li>
            ))}
          </ul>
          <p className="ui-label text-ink-muted">
            Newest first, exactly the rows the store answered — nothing is re-sorted or invented.
          </p>
        </>
      ) : null}
    </section>
  );
}

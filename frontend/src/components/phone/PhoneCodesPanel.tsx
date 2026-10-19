"use client";

import { useCallback, useEffect, useId, useRef, useState } from "react";

import { Button, SectionHeader } from "@/components/ui";

import {
  CODES_LIMIT,
  codeWhenText,
  fetchCodes,
  isPhoneUnavailable,
  maskCode,
  phoneErrorText,
  type PhoneCode,
  type PhoneCodeList,
} from "./phoneApi";
import { PhoneUnavailable } from "./PhoneStates";

/**
 * The `/phone` CODES panel (wave V19) — the owner-facing consumer of the audited
 * `GET /api/v1/phone/codes` read, under the recorded owner amendment: a dedicated panel is
 * allowed, and it keeps the original law (codes are never in the activity feed / dashboard cards —
 * this surface is the ONLY place they can appear, and only after an explicit click).
 *
 * The privacy laws this file keeps, and why each one is load-bearing:
 *
 *   - FETCH-ON-CLICK ONLY. Nothing reads on mount, on window change, on expand, or on a poll.
 *     The route writes an `activity_log` row per call; an auto-read would turn opening a page into
 *     an audit event. The single fetch path is `fetchCodes` on the CTA's onClick.
 *   - MASKED BY DEFAULT. `maskCode` is a FIXED three-dot run + the last two characters, so the
 *     mask cannot leak the code's length. Reveal is per row, `aria-pressed`, and returns to the
 *     mask on blur-out, on Escape, on Copy, on refetch, on collapse and on unmount.
 *   - COPY IS THE FULL CODE. `navigator.clipboard.writeText(row.code)` writes the real value even
 *     though the screen shows the mask, then re-masks immediately. A refused clipboard names
 *     itself inline; it never fails silently and never leaves the code revealed.
 *   - NOTHING IS PERSISTED OR LOGGED. This file makes no storage call and no console call. The
 *     rows live in `useState` and die with the component; there are no timers to leave behind.
 *   - THE SNIPPET IS NEVER RENDERED. `snippet` is the message body the code was found in — it
 *     contains the code in clear text, so rendering it beside a mask would defeat the mask.
 *   - EMPTY / REFUSED / UNAVAILABLE ARE NAMED. A successful empty read says "no codes within N h";
 *     `E_PHONE_UNAVAILABLE` renders the page's one PHONE LINK UNAVAILABLE block (never an empty
 *     list); any other refusal renders its `code: message — how_to_fix` inline.
 *
 * Keyboard / a11y: every control is a real `<button>`, the window selector is a labelled group of
 * `aria-pressed` toggles, Reveal/Hide is `aria-pressed`, Escape re-masks a revealed row and then
 * collapses the panel (clearing every row), the copied/empty notes are `role="status"`. No
 * transition or animation classes — reduced-motion safe by construction.
 */

type CodesWindow = 1 | 6 | 24 | null;

const WINDOW_OPTIONS: { value: CodesWindow; label: string; testId: string }[] = [
  { value: 1, label: "1 h", testId: "phone-codes-window-1" },
  { value: 6, label: "6 h", testId: "phone-codes-window-6" },
  { value: 24, label: "24 h", testId: "phone-codes-window-24" },
  { value: null, label: "All", testId: "phone-codes-window-all" },
];

/** The default window: the 2FA use case is "the code I just received", and 24 h is the widest
 *  window that still reads as "recent" — the selector states it, the fetch sends it. */
const DEFAULT_WINDOW: CodesWindow = 24;

type CodesRead =
  | { kind: "idle" }
  | { kind: "loading" }
  | { kind: "ok"; data: PhoneCodeList; window: CodesWindow }
  | { kind: "unavailable"; detail: string }
  | { kind: "failed"; detail: string };

function windowWord(window: CodesWindow): string {
  return window === null ? "with no window" : `within ${window} h`;
}

function emptyText(window: CodesWindow): string {
  return window === null
    ? "No verification codes in the store."
    : `No verification codes within ${window} h.`;
}

export function PhoneCodesPanel() {
  const bodyId = useId();
  const [open, setOpen] = useState(true);
  const [window_, setWindow] = useState<CodesWindow>(DEFAULT_WINDOW);
  const [state, setState] = useState<CodesRead>({ kind: "idle" });
  const [revealed, setRevealed] = useState<number | null>(null);
  const [copied, setCopied] = useState<number | null>(null);
  const [copyError, setCopyError] = useState<{ index: number; detail: string } | null>(null);

  // One "is this component still mounted" flag, one request generation counter: a response that
  // arrives after unmount or after the window changed is DROPPED, never rendered under the wrong
  // window. No timers are involved — every reset is synchronous state.
  const alive = useRef(true);
  const request = useRef(0);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);

  const resetDisclosure = useCallback(() => {
    setRevealed(null);
    setCopied(null);
    setCopyError(null);
  }, []);

  const read = useCallback(async () => {
    const generation = ++request.current;
    resetDisclosure();
    setState({ kind: "loading" });
    try {
      const data = await fetchCodes(CODES_LIMIT, window_);
      if (!alive.current || request.current !== generation) return;
      setState({ kind: "ok", data, window: window_ });
    } catch (e: unknown) {
      if (!alive.current || request.current !== generation) return;
      setState(
        isPhoneUnavailable(e)
          ? { kind: "unavailable", detail: phoneErrorText(e) }
          : { kind: "failed", detail: phoneErrorText(e) },
      );
    }
  }, [resetDisclosure, window_]);

  const chooseWindow = useCallback(
    (next: CodesWindow) => {
      if (next === window_) return;
      // A row read in one window must never sit under another: changing the window CLEARS the
      // rows and cancels any in-flight read, so the next click is a fresh, explicit fetch.
      request.current += 1;
      setWindow(next);
      resetDisclosure();
      setState({ kind: "idle" });
    },
    [resetDisclosure, window_],
  );

  const collapse = useCallback(() => {
    request.current += 1;
    resetDisclosure();
    setState({ kind: "idle" });
    setOpen(false);
  }, [resetDisclosure]);

  const onKeyDown = (event: React.KeyboardEvent<HTMLElement>) => {
    if (event.key !== "Escape") return;
    if (revealed !== null) {
      // First Escape belongs to the reveal: mask it, keep the panel open.
      event.preventDefault();
      setRevealed(null);
      return;
    }
    if (open) {
      event.preventDefault();
      collapse();
    }
  };

  const copy = useCallback(async (row: PhoneCode, index: number) => {
    setCopyError(null);
    setCopied(null);
    try {
      if (
        typeof navigator === "undefined" ||
        !navigator.clipboard ||
        typeof navigator.clipboard.writeText !== "function"
      ) {
        throw new Error("navigator.clipboard.writeText is unavailable in this context");
      }
      // The FULL code, even while the row shows the mask.
      await navigator.clipboard.writeText(row.code);
      if (!alive.current) return;
      setRevealed(null); // re-mask immediately — a copy must not leave a code on screen
      setCopied(index);
    } catch (e: unknown) {
      if (!alive.current) return;
      setRevealed(null);
      setCopyError({ index, detail: phoneErrorText(e) });
    }
  }, []);

  const rows = state.kind === "ok" ? state.data.codes : [];
  const dropped = state.kind === "ok" ? state.data.dropped : 0;

  return (
    <section
      aria-label="Verification codes"
      data-testid="phone-codes"
      className="space-y-2"
      onKeyDown={onKeyDown}
    >
      <SectionHeader
        kicker="CODES"
        title="Verification codes"
        action={
          <Button
            size="sm"
            data-testid="phone-codes-toggle"
            aria-expanded={open}
            aria-controls={bodyId}
            onClick={() => (open ? collapse() : setOpen(true))}
          >
            {open ? "Collapse" : "Expand"}
          </Button>
        }
      />

      <p data-testid="phone-codes-audit-note" className="ui-label text-ink-muted">
        Every read writes one audit row — count and window only; code values are never logged.
        Nothing is read until you ask.
      </p>

      <div id={bodyId} className="space-y-2">
        {open ? (
          <>
            <p data-testid="phone-codes-limit-note" className="ui-label text-ink-muted">
              {`Up to the newest ${CODES_LIMIT} codes — the route's own cap, no paging.`}
            </p>
            <div role="group" aria-label="Codes window" className="flex flex-wrap items-center gap-1">
              <span className="ui-label mr-1 text-ink-muted">Window</span>
              {WINDOW_OPTIONS.map((option) => (
                <Button
                  key={option.label}
                  size="sm"
                  data-testid={option.testId}
                  aria-pressed={window_ === option.value}
                  onClick={() => chooseWindow(option.value)}
                >
                  {option.label}
                </Button>
              ))}
            </div>

            <div className="flex flex-wrap items-center gap-2">
              <Button
                data-testid="phone-codes-read"
                disabled={state.kind === "loading"}
                onClick={() => void read()}
              >
                Read verification codes
              </Button>
              <span className="ui-label text-ink-muted">
                Held in this panel&apos;s memory only — never stored, never logged; collapsing or
                leaving the page clears them.
              </span>
            </div>

            {state.kind === "loading" ? (
              // The repo's skeleton idiom, with `motion-reduce:animate-none`: the panel adds no
              // motion of its own and this is the one place it could have inherited some.
              <div className="space-y-2" aria-busy="true" data-testid="phone-codes-loading">
                {Array.from({ length: 2 }).map((_, index) => (
                  <div
                    key={index}
                    className="h-4 animate-pulse bg-surface-strong motion-reduce:animate-none"
                  />
                ))}
              </div>
            ) : null}

            {state.kind === "unavailable" ? (
              <PhoneUnavailable testId="phone-codes-unavailable" detail={state.detail} />
            ) : null}

            {state.kind === "failed" ? (
              <p role="alert" data-testid="phone-codes-error" className="text-sm text-alert">
                {`Could not read codes: ${state.detail}`}
              </p>
            ) : null}

            {state.kind === "ok" ? (
              <>
                {dropped > 0 ? (
                  <p data-testid="phone-codes-dropped" className="ui-label text-ink-muted">
                    {`${dropped} malformed ${dropped === 1 ? "row was" : "rows were"} dropped — they never render as codes.`}
                  </p>
                ) : null}

                {rows.length === 0 ? (
                  <p data-testid="phone-codes-empty" className="ui-label text-ink-muted">
                    {emptyText(state.window)}
                  </p>
                ) : (
                  <ul className="space-y-1">
                    {rows.map((row, index) => {
                      const isRevealed = revealed === index;
                      return (
                        <li
                          key={`${index}-${row.from}-${row.when ?? "none"}`}
                          data-testid="phone-code-row"
                          className="flex flex-wrap items-baseline gap-2 text-sm"
                          onBlur={(event) => {
                            // Focus leaving the ROW (not merely the Reveal button) re-masks it.
                            const next = event.relatedTarget as Node | null;
                            if (revealed === index && !event.currentTarget.contains(next)) {
                              setRevealed(null);
                            }
                          }}
                        >
                          <span className="ui-label text-ink-muted">{codeWhenText(row.when)}</span>
                          <span className="font-bold text-ink">{row.from}</span>
                          <span
                            data-testid="phone-code-value"
                            className="font-mono text-sm tracking-wider text-ink"
                          >
                            {isRevealed ? row.code : maskCode(row.code)}
                          </span>
                          <Button
                            size="sm"
                            data-testid="phone-code-reveal"
                            aria-pressed={isRevealed}
                            aria-label={`${isRevealed ? "Hide" : "Reveal"} code from ${row.from}`}
                            onClick={() => setRevealed(isRevealed ? null : index)}
                          >
                            {isRevealed ? "Hide" : "Reveal"}
                          </Button>
                          <Button
                            size="sm"
                            data-testid="phone-code-copy"
                            aria-label={`Copy code from ${row.from}`}
                            onClick={() => void copy(row, index)}
                          >
                            Copy
                          </Button>
                          {copied === index ? (
                            <span
                              role="status"
                              data-testid="phone-code-copied"
                              className="ui-label text-ink-muted"
                            >
                              Copied — the code is masked again.
                            </span>
                          ) : null}
                          {copyError !== null && copyError.index === index ? (
                            <span
                              role="alert"
                              data-testid="phone-code-copy-error"
                              className="text-sm text-alert"
                            >
                              {`Copy refused: ${copyError.detail} — the code is masked again; reveal and copy by hand if you need it.`}
                            </span>
                          ) : null}
                        </li>
                      );
                    })}
                  </ul>
                )}

                <p className="ui-label text-ink-muted">
                  {`Read ${windowWord(state.window)} — the surrounding message text is never shown; it carries the code in clear text.`}
                </p>
              </>
            ) : null}
          </>
        ) : (
          <p data-testid="phone-codes-collapsed" className="ui-label text-ink-muted">
            Collapsed — this panel holds no codes while it is closed.
          </p>
        )}
      </div>
    </section>
  );
}

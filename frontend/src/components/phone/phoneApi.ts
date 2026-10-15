import { ApiError } from "@/lib/api";

/**
 * The H-P1 wire contract (wave V16, frozen interface) — types, envelope unwrap and validators for
 * every `GET /api/v1/phone/*` read the phone UI performs. ONE module so the dashboard card and the
 * `/phone` page cannot drift apart in how they read the same routes.
 *
 * The rules encoded here, in the honesty voice the rest of the frontend uses:
 *
 *   - every success body is the envelope `{ok, data}` — `unwrapEnvelope` refuses (by name) anything
 *     else, so a future shape change surfaces as a NAMED failure instead of `undefined` rendering;
 *   - a missing store is the named 503 `E_PHONE_UNAVAILABLE`; a 200 whose `store_status` is
 *     `"unavailable"` is an ANSWER, not data — both map to the same honest state
 *     (`PHONE LINK UNAVAILABLE`), never to a zero-row render;
 *   - a field of the wrong type is NOT coerced: a null `unread_total` fails validation ("numeric
 *     unread_total") instead of printing "0 unread". Nulls that the contract EXPECTS (`last_when`,
 *     `dur_s`) are type-legal and render as stated unknowns;
 *   - `when`/`last_when` strings are shown VERBATIM — the API's own wording; this module never
 *     re-parses a phone timestamp (the client has no clock authority over the store).
 */

/** The one message window this UI reads — printed in the page's window note, sent as `limit`. */
export const MESSAGES_WINDOW = 100;
/** The one calls window this UI reads — sent as `limit`. */
export const CALLS_WINDOW = 50;

/** One summary thread row — H-P1 `threads[]`. `"with"` is the display name the service resolved;
 *  `last_when` is `str|null` on the wire (null = no time the service could state). */
export type PhoneThreadRow = {
  thread_id: number;
  with: string;
  unread: number;
  last_when: string | null;
  last_text: string;
};

/** One missed call today — H-P1 `missed_calls_today[]`. `when` renders verbatim. */
export type PhoneMissedCall = {
  who: string;
  num_masked: string;
  when: string;
};

/** `GET /phone/summary` → `data`. `store_status` is an open string on purpose: only the literal
 *  `"ok"` unlocks the rows; anything else (today only `"unavailable"`) is the honest unavailable
 *  state — the contract's declared `"unavailable"` and any future word behave the same way. */
export type PhoneSummary = {
  store_status: string;
  unread_total: number;
  threads: PhoneThreadRow[];
  missed_calls_today: PhoneMissedCall[];
};

/** One message row — H-P1 `messages[]`. `dir` is the store's own direction word ("in"/"out"/…),
 *  rendered as-is rather than translated. */
export type PhoneMessage = {
  when: string;
  dir: string;
  who: string;
  text: string;
  thread_id: number;
};

/** One call row — H-P1 `calls[]`. `dur_s` is `number` on the declared contract; `null`/absent is
 *  TOLERATED here (a missed call has no duration to state) and renders as the em dash — never 0. */
export type PhoneCall = {
  when: string;
  type: string;
  who: string;
  num_masked: string;
  dur_s: number | null | undefined;
};

/* ── envelope + validation ─────────────────────────────────────────────────────────────────── */

function fail(what: string, field: string): never {
  throw new Error(`the phone ${what} read answered ok without ${field}`);
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** Unwraps the H-P1 `{ok, data}` envelope; anything else is refused BY NAME. */
export function unwrapEnvelope(what: string, body: unknown): unknown {
  if (isRecord(body) && body.ok === true && isRecord(body.data)) return body.data;
  fail(what, "its {ok, data} envelope");
}

/** Validates `data` of `GET /phone/summary`; every failed check names the exact field. */
export function parseSummary(data: unknown): PhoneSummary {
  if (!isRecord(data)) fail("summary", "an object body");
  const { store_status, unread_total, threads, missed_calls_today } = data as Record<string, unknown>;
  if (typeof store_status !== "string") fail("summary", "a string store_status");
  if (typeof unread_total !== "number") fail("summary", "a numeric unread_total");
  if (!Array.isArray(threads)) fail("summary", "a threads array");
  const parsedThreads = threads.map((row): PhoneThreadRow => {
    if (!isRecord(row)) fail("summary", "object thread rows");
    if (typeof row.thread_id !== "number") fail("summary", "a numeric thread_id");
    if (typeof row.with !== "string") fail("summary", 'a string "with"');
    if (typeof row.unread !== "number") fail("summary", "a numeric thread unread");
    if (row.last_when !== null && typeof row.last_when !== "string") {
      fail("summary", "a string-or-null last_when");
    }
    if (typeof row.last_text !== "string") fail("summary", "a string last_text");
    return {
      thread_id: row.thread_id,
      with: row.with,
      unread: row.unread,
      last_when: row.last_when as string | null,
      last_text: row.last_text,
    };
  });
  if (!Array.isArray(missed_calls_today)) fail("summary", "a missed_calls_today array");
  const parsedMissed = missed_calls_today.map((row): PhoneMissedCall => {
    if (!isRecord(row)) fail("summary", "object missed-call rows");
    if (typeof row.who !== "string") fail("summary", "a string who");
    if (typeof row.num_masked !== "string") fail("summary", "a string num_masked");
    if (typeof row.when !== "string") fail("summary", "a string when");
    return { who: row.who, num_masked: row.num_masked, when: row.when };
  });
  return {
    store_status,
    unread_total,
    threads: parsedThreads,
    missed_calls_today: parsedMissed,
  };
}

/** Validates `data` of `GET /phone/messages` into the rows array (`data.messages`). */
export function parseMessages(data: unknown): PhoneMessage[] {
  if (!isRecord(data)) fail("messages", "an object body");
  const { messages } = data as Record<string, unknown>;
  if (!Array.isArray(messages)) fail("messages", "a messages array");
  return messages.map((row): PhoneMessage => {
    if (!isRecord(row)) fail("messages", "object message rows");
    if (typeof row.when !== "string") fail("messages", "a string when");
    if (typeof row.dir !== "string") fail("messages", "a string dir");
    if (typeof row.who !== "string") fail("messages", "a string who");
    if (typeof row.text !== "string") fail("messages", "a string text");
    if (typeof row.thread_id !== "number") fail("messages", "a numeric thread_id");
    return {
      when: row.when,
      dir: row.dir,
      who: row.who,
      text: row.text,
      thread_id: row.thread_id,
    };
  });
}

/** Validates `data` of `GET /phone/calls` into the rows array (`data.calls`). */
export function parseCalls(data: unknown): PhoneCall[] {
  if (!isRecord(data)) fail("calls", "an object body");
  const { calls } = data as Record<string, unknown>;
  if (!Array.isArray(calls)) fail("calls", "a calls array");
  return calls.map((row): PhoneCall => {
    if (!isRecord(row)) fail("calls", "object call rows");
    if (typeof row.when !== "string") fail("calls", "a string when");
    if (typeof row.type !== "string") fail("calls", "a string type");
    if (typeof row.who !== "string") fail("calls", "a string who");
    if (typeof row.num_masked !== "string") fail("calls", "a string num_masked");
    if (row.dur_s !== undefined && row.dur_s !== null && typeof row.dur_s !== "number") {
      fail("calls", "a numeric-or-null dur_s");
    }
    return {
      when: row.when,
      type: row.type,
      who: row.who,
      num_masked: row.num_masked,
      dur_s: row.dur_s as number | null | undefined,
    };
  });
}

/* ── failure classification ────────────────────────────────────────────────────────────────── */

/** `E_PHONE_UNAVAILABLE` is a CAPABILITY absence (the store is not mounted), not a fault: it maps
 *  to its own honest state. The instanceof guard mirrors WebsiteCard's — suites that factory-mock
 *  `@/lib/api` leave `ApiError` undefined, and instanceof against undefined throws. */
export function isPhoneUnavailable(e: unknown): boolean {
  return typeof ApiError !== "undefined" && e instanceof ApiError && e.code === "E_PHONE_UNAVAILABLE";
}

/** The failure's own words: an ApiError's `code: message — how_to_fix`, else the error's message. */
export function phoneErrorText(e: unknown): string {
  if (typeof ApiError !== "undefined" && e instanceof ApiError) return e.display;
  if (e instanceof Error) return e.message;
  try {
    return JSON.stringify(e) ?? String(e);
  } catch {
    return String(e);
  }
}

/* ── the `?thread=` param ──────────────────────────────────────────────────────────────────── */

/** `/phone?thread=N` — digits only, a positive safe integer; everything else is "no thread in the
 *  URL" (the documented no-op, the same parse the tasks page applies to its `?task=` param). */
export function parseThreadParam(raw: string | null): number | null {
  if (raw === null || !/^\d+$/.test(raw)) return null;
  const value = Number(raw);
  return Number.isSafeInteger(value) && value > 0 ? value : null;
}

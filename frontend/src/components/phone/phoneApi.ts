import { ApiError, api } from "@/lib/api";

/**
 * The H-P1 wire contract (wave V16, frozen interface) plus the V17 SEND client — types, envelope
 * unwrap and validators for every `GET /api/v1/phone/*` read the phone UI performs, and for
 * `POST /api/v1/phone/send`. ONE module so the dashboard card, the `/phone` page and the send
 * composer cannot drift apart in how they read (or refuse) the same surface.
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

/* ── the CODES surface (wave V19, frozen route: GET /phone/codes) ────────────────────────── */

/** The route's own default row cap (1..50 on the wire). The panel states this number. */
export const CODES_LIMIT = 10;

/** One verification code row — `GET /phone/codes` → `data.codes[]`, field for field. `when` is
 *  `str|null` on the wire (`filetime_to_local` returns None for a 0/absent store timestamp), so a
 *  null is a real value and renders as the app-wide "time unknown" — never an invented instant.
 *  `snippet` is the message text the code was found in; the panel deliberately NEVER renders it
 *  (it contains the code in clear text and would defeat the mask). */
export type PhoneCode = {
  when: string | null;
  from: string;
  code: string;
  snippet: string;
};

/** `parseCodes`'s result: the rows that passed validation, plus how many malformed rows were
 *  DROPPED. Dropping is counted so the panel can say it out loud instead of silently shrinking. */
export type PhoneCodeList = {
  codes: PhoneCode[];
  dropped: number;
};

/** Validates one code row. Returns null (drop) instead of throwing: the codes route serves a LIST,
 *  and one corrupt row must not hide the codes that are real. Every field is checked by type —
 *  a numeric `code` is never coerced into a string. */
function parseCodeRow(row: unknown): PhoneCode | null {
  if (!isRecord(row)) return null;
  const { when, from, code, snippet } = row;
  if (when !== null && typeof when !== "string") return null;
  if (typeof from !== "string") return null;
  if (typeof code !== "string" || code.trim() === "") return null;
  if (typeof snippet !== "string") return null;
  return { when: when as string | null, from, code, snippet };
}

/** Validates `data` of `GET /phone/codes`. The `codes` ARRAY is required (a body without one is
 *  refused by name — that is a contract change, not a row problem); individual malformed rows are
 *  dropped and counted. */
export function parseCodes(data: unknown): PhoneCodeList {
  if (!isRecord(data)) fail("codes", "an object body");
  const { codes } = data as Record<string, unknown>;
  if (!Array.isArray(codes)) fail("codes", "a codes array");
  const parsed: PhoneCode[] = [];
  let dropped = 0;
  for (const row of codes) {
    const code = parseCodeRow(row);
    if (code === null) dropped += 1;
    else parsed.push(code);
  }
  return { codes: parsed, dropped };
}

/** THE REAL codes read — the panel's ONLY fetch path (fetch-on-click; nothing auto-reads). Same
 *  `api.get` + envelope pattern as every other phone read, so the key header, timeout and error
 *  taxonomy are the shared ones. `withinHours` absent/null = the route's own "no window". */
export async function fetchCodes(
  limit: number,
  withinHours?: number | null,
): Promise<PhoneCodeList> {
  const path =
    withinHours === undefined || withinHours === null
      ? `/phone/codes?limit=${limit}`
      : `/phone/codes?limit=${limit}&within_hours=${withinHours}`;
  const payload = await api.get<unknown>(path);
  return parseCodes(unwrapEnvelope("codes", payload));
}

/** The masked form of a code: a FIXED three-dot run plus its last two characters (`•••93`).
 *  Fixed width on purpose — a mask that tracks the code's length leaks the length. A code of two
 *  characters or fewer reveals NOTHING (the whole mask stays dots). Empty in, empty out. */
export function maskCode(code: string): string {
  if (code.trim() === "") return "";
  if (code.length <= 2) return "•••";
  return `•••${code.slice(-2)}`;
}

/** The store's `when`, or the app-wide honest placeholder when it carries no time. */
export function codeWhenText(when: string | null): string {
  return when === null || when.trim() === "" ? "time unknown" : when;
}

/* ── the SEND contract (wave V17, frozen) ────────────────────────────────────────────────── */

/** The composer cap the server enforces too (`BODY_MAX` in `phone_send_service.py`). */
export const SEND_BODY_MAX = 640;
/** The one driver name the server accepts; a different one is not a send this UI can trust. */
export const PHONE_SEND_DRIVER = "cdp-google-messages";

/** `POST /phone/send` → `data`. The server has already observed the send; nothing here is local. */
export type PhoneSendResult = {
  sent_at: string;
  to_masked: string;
  driver: string;
};

/** Codes that mean "this deployment cannot send right now" — the composer disables and SAYS WHY.
 *  `E_VALIDATION` is deliberately absent: a malformed message is fixable in the form, so the
 *  composer stays enabled and the refusal renders inline. */
export const SEND_BLOCKING_CODES = [
  "E_SANDBOX_BLOCKED",
  "E_PHONE_SEND_UNPAIRED",
  "E_PHONE_UNAVAILABLE",
] as const;

/** The refusal vocabulary of `POST /phone/send`, in the user's words. Every branch names its code
 *  — a send surface that fails silently is worse than one that refuses. */
const SEND_REASONS: Record<string, string> = {
  E_SANDBOX_BLOCKED:
    "Sending is blocked (E_SANDBOX_BLOCKED): SANDBOX_MODE is on — this deployment sends no real SMS.",
  E_PHONE_SEND_UNPAIRED:
    "Sending is blocked (E_PHONE_SEND_UNPAIRED): no paired Google Messages web session — pair the phone in the automation Chrome.",
  E_PHONE_UNAVAILABLE:
    "Sending is blocked (E_PHONE_UNAVAILABLE): the send driver is unreachable (CDP or driver missing).",
};

/** Validates `data` of `POST /phone/send`. A result that names a different driver is refused —
 *  the UI only claims a send the documented driver reported. */
export function parseSendResult(data: unknown): PhoneSendResult {
  const refuse = (field: string): never => {
    throw new Error(`the phone send result did not carry ${field}`);
  };
  if (!isRecord(data)) refuse("an object body");
  const { sent_at, to_masked, driver } = data as Record<string, unknown>;
  if (typeof sent_at !== "string" || sent_at.trim() === "") refuse("a string sent_at");
  if (typeof to_masked !== "string" || to_masked.trim() === "") refuse("a string to_masked");
  if (driver !== PHONE_SEND_DRIVER) refuse(`the "${PHONE_SEND_DRIVER}" driver declaration`);
  return { sent_at: sent_at as string, to_masked: to_masked as string, driver: PHONE_SEND_DRIVER };
}

/** The REAL send call — `api.post`, one attempt, no retry (a blind retry could double-text). */
export async function sendSms(to: string, body: string): Promise<PhoneSendResult> {
  const payload = await api.post<unknown>("/phone/send", { to, body });
  return parseSendResult(unwrapEnvelope("send", payload));
}

/** The refusal code on an ApiError, or null for anything else (a transport failure has no code). */
export function sendRefusalCode(e: unknown): string | null {
  if (typeof ApiError !== "undefined" && e instanceof ApiError) return e.code;
  return null;
}

/** Does this refusal mean the composer must stay disabled until the deployment changes? */
export function isSendBlocked(e: unknown): boolean {
  const code = sendRefusalCode(e);
  return code !== null && (SEND_BLOCKING_CODES as readonly string[]).includes(code);
}

/** The NAMED reason shown next to a disabled Send button (empty string = not a blocking code). */
export function sendBlockedReason(code: string | null): string {
  return code === null ? "" : (SEND_REASONS[code] ?? "");
}

/** `sent at HH:MM` from the server's own ISO timestamp — no timezone re-derivation: the clock
 *  digits are read from the string, and a non-ISO timestamp renders VERBATIM instead of guessing. */
export function sentClock(sentAt: string): string {
  const match = /T(\d{2}:\d{2})/.exec(sentAt);
  return match ? match[1] : sentAt;
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

/* ── the MEDIA surface (wave V18, frozen: list + stream) ─────────────────────────────────── */

/** The list route's own cap — the service returns at most this many parts, newest window. */
export const MEDIA_CAP = 50;

/** The client's inline-preview ceiling. Above it a part is offered as a DOWNLOAD with the size
 *  stated, because auto-fetching a hundreds-of-megabytes blob into an object URL would stall the
 *  tab — the store's own `size_bytes` is what this compares, never an estimate. */
export const MEDIA_PREVIEW_MAX_BYTES = 32 * 1024 * 1024;

/** One media part — `GET /phone/media?thread_id=N` → `data.parts[]`, field for field. `when` is
 *  `str|null` on the wire — the service's `filetime_to_local` answers None for a 0/absent store
 *  timestamp (V19 item 2: the parser used to refuse that legal value and the whole list read as a
 *  failure). A null renders as the app-wide "time unknown"; a non-empty string renders VERBATIM
 *  (this client has no clock authority). */
export type PhoneMediaPart = {
  part_id: number;
  content_type: string;
  size_bytes: number;
  when: string | null;
  thread_id: number;
};

/** `GET /phone/media?thread_id=N` → `data`. `truncated` is the service's own statement that the
 *  thread holds more parts than the 50 it returned; it is rendered by name, never inferred. */
export type PhoneMediaList = {
  parts: PhoneMediaPart[];
  truncated: boolean;
};

/** Validates the media list `data`; every failed check names the exact field (a string `part_id`
 *  must never render as a row). */
export function parseMediaList(data: unknown): PhoneMediaList {
  if (!isRecord(data)) fail("media", "an object body");
  const { parts, truncated } = data as Record<string, unknown>;
  if (!Array.isArray(parts)) fail("media", "a parts array");
  if (typeof truncated !== "boolean") fail("media", "a boolean truncated");
  const parsed = parts.map((row): PhoneMediaPart => {
    if (!isRecord(row)) fail("media", "object media part rows");
    if (typeof row.part_id !== "number") fail("media", "a numeric part_id");
    if (typeof row.content_type !== "string") fail("media", "a string content_type");
    if (typeof row.size_bytes !== "number") fail("media", "a numeric size_bytes");
    if (row.when !== null && typeof row.when !== "string") fail("media", "a string-or-null when");
    if (typeof row.thread_id !== "number") fail("media", "a numeric thread_id");
    return {
      part_id: row.part_id,
      content_type: row.content_type,
      size_bytes: row.size_bytes,
      when: row.when as string | null,
      thread_id: row.thread_id,
    };
  });
  return { parts: parsed, truncated };
}

/** Byte count in the UI's KB/MB voice — one decimal, the unit chosen by the real value. A
 *  non-finite/negative size is "size unknown" (never a fabricated 0). */
export function mediaSizeText(sizeBytes: number): string {
  if (!Number.isFinite(sizeBytes) || sizeBytes < 0) return "size unknown";
  if (sizeBytes < 1024) return `${Math.round(sizeBytes)} B`;
  const kb = sizeBytes / 1024;
  // 1023.95 KB would round to "1024.0 KB"; the honest unit there is MB.
  if (kb < 1023.95) return `${kb.toFixed(1)} KB`;
  return `${(sizeBytes / (1024 * 1024)).toFixed(1)} MB`;
}

/** The store's `when`, or the stated placeholder when it carries no time — never an invented one.
 *  `null` is a legal wire value (V19 item 2) and gets the same honest placeholder as an empty
 *  string; this function is the only place the media surface turns a timestamp into words. */
export function mediaWhenText(when: string | null): string {
  return when === null || when.trim() === "" ? "time unknown" : when;
}

/** Does this part render as an inline image? */
export function isImageType(contentType: string): boolean {
  return /^image\//i.test(contentType);
}

/** Does this part render as inline audio? */
export function isAudioType(contentType: string): boolean {
  return /^audio\//i.test(contentType);
}

/** The extension the server's own `phone-part-<id>.<ext>` contract uses, for the fallback name
 *  when a `Content-Disposition` header is absent (the bytes are still the server's). */
const MEDIA_EXTENSIONS: Record<string, string> = {
  "image/jpeg": "jpg",
  "image/png": "png",
  "image/gif": "gif",
  "image/webp": "webp",
  "image/heic": "heic",
  "audio/mp4": "m4a",
  "audio/mpeg": "mp3",
  "audio/ogg": "ogg",
  "audio/wav": "wav",
  "audio/webm": "weba",
  "application/pdf": "pdf",
  "text/plain": "txt",
};

/** The save name for one part: the server's `Content-Disposition` filename when it sent one, else
 *  the stated `phone-part-<id>.<ext>` fallback (unknown types save as `.bin`). */
export function mediaFilename(
  disposition: string | null,
  partId: number,
  contentType: string,
): string {
  if (disposition) {
    const match = /filename\s*=\s*"?([^";]+)"?/i.exec(disposition);
    if (match && match[1].trim() !== "") return match[1].trim();
  }
  const base = contentType.split(";")[0].trim().toLowerCase();
  return `phone-part-${partId}.${MEDIA_EXTENSIONS[base] ?? "bin"}`;
}

/** The refusal carried by a response body, in the shapes `lib/api.ts` parses (the `{ok:false,…}`
 *  envelope and FastAPI's `{detail:{…}}`). Exported because the media stream is a raw blob fetch
 *  (`lib/api.ts` is JSON-only by construction) and must read refusals exactly like the export
 *  download does. */
export function parseRefusalBody(body: unknown): { code: string; message: string; howToFix: string } | null {
  if (!body || typeof body !== "object") return null;
  const b = body as { ok?: unknown; error?: unknown; detail?: unknown };
  const candidate =
    b.ok === false && b.error !== null && typeof b.error === "object"
      ? (b.error as Record<string, unknown>)
      : b.detail !== null && typeof b.detail === "object"
        ? (b.detail as Record<string, unknown>)
        : null;
  if (!candidate || typeof candidate.code !== "string") return null;
  return {
    code: candidate.code,
    message: typeof candidate.message === "string" ? candidate.message : candidate.code,
    howToFix: typeof candidate.how_to_fix === "string" ? candidate.how_to_fix : "",
  };
}

/** One refusal in the same `code: message — how_to_fix` voice `ApiError.display` uses. */
export function refusalText(refusal: { code: string; message: string; howToFix: string }): string {
  return refusal.howToFix
    ? `${refusal.code}: ${refusal.message} — ${refusal.howToFix}`
    : `${refusal.code}: ${refusal.message}`;
}

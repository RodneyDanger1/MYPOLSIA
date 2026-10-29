/**
 * F-41 — the task row's ATTEMPT HISTORY: the live `attempts` counter and the RETIRED attempt error
 * (`last_attempt_error`), as a narrow, defensive read shape.
 *
 * Why this module exists: the backend has served both keys since A-W4-02
 * (`backend/app/schemas/task.py`: `attempts` is the real column; `last_attempt_error` is computed
 * from `metadata.last_attempt_error`, bounded to 2,000 wire characters and typed by
 * `TaskLastAttemptError`), and `task_service._retire_error_message` deliberately MOVES a completed
 * row's superseded `error_message` into that bag key so `completed` and the served error agree. The
 * record was therefore write-only from the owner's side: the task screen had no consumer at all
 * (visuals F-41, live `/tasks/313`). This is the consumer's parsing half.
 *
 * The rules below are the honesty rules of `runTruth.ts`, applied to a failure record:
 *   * a value renders only when the wire really carried it — a non-integer, a string, a boolean or
 *     a missing key is ABSENCE, never a coerced `0`/`"3"`/`true`;
 *   * `truncated` comes from the server (`message_chars > 2000` cut), and this module never invents
 *     a second cut: it exposes the stored length so the screen can state what it is showing;
 *   * the record is HISTORY. Nothing here may be rendered as the row's current error — that is
 *     `error_message`, which the retirement writer CLEARS precisely so the two never disagree.
 */

/** The retired record as this surface renders it. Every field is a fact the wire served. */
export type RetiredAttemptError = {
  /** The failure text the superseded attempt recorded (the wire's own ≤2,000-char read). */
  message: string;
  /** The STORED length (`message_chars`) — so a cut read is measurable, never a guess. */
  messageChars: number;
  /** True exactly when the wire cut the text (`message_chars` > the served message length). */
  truncated: boolean;
  /** The execution count AT retirement, when the writer recorded one. */
  attempts: number | null;
  /** When the failure was retired (ISO-8601 as written), when recorded. */
  retiredAt: string | null;
  /** Which writer retired it, when recorded. */
  retiredBy: string | null;
  /** The transition that ended the failure (`completed`, …) — what makes this HISTORY. */
  retiredOnStatus: string | null;
};

/** A non-negative integer, or `null` — the one rule for every count on this surface (`0` is a
 *  measured zero: "no delivery", never an unknown). Booleans are NOT integers here. */
export function attemptCount(value: unknown): number | null {
  if (typeof value !== "number" || !Number.isFinite(value)) return null;
  if (!Number.isInteger(value) || value < 0) return null;
  return value;
}

function optionalText(value: unknown): string | null {
  return typeof value === "string" && value.trim() !== "" ? value : null;
}

/**
 * The row's retired attempt error, or `null` when the wire carried no usable record.
 *
 * Defensive by contract (the bag is a hand-editable JSON column): a non-object, a record with no
 * non-empty `message`, or one whose typed fields have the wrong shape reads as `null` — the same
 * "junk is absence, never a half-record presented as fact" rule the server applies on its side.
 */
export function retiredAttemptError(
  row: { last_attempt_error?: unknown } | null | undefined,
): RetiredAttemptError | null {
  const raw = row?.last_attempt_error;
  if (raw === null || raw === undefined) return null;
  if (typeof raw !== "object" || Array.isArray(raw)) return null;
  const record = raw as Record<string, unknown>;
  const message = typeof record.message === "string" ? record.message : "";
  if (message === "") return null;

  const storedChars = attemptCount(record.message_chars);
  const messageChars = storedChars !== null && storedChars >= message.length ? storedChars : message.length;
  return {
    message,
    messageChars,
    // The server's own flag when it sent one; otherwise the length arithmetic is the same fact.
    truncated: record.truncated === true || messageChars > message.length,
    attempts: attemptCount(record.attempts),
    retiredAt: optionalText(record.retired_at),
    retiredBy: optionalText(record.retired_by),
    retiredOnStatus: optionalText(record.retired_on_status),
  };
}

/**
 * Whether this row has attempt HISTORY worth a section on the task screen.
 *
 * A retired record always qualifies. The counter alone qualifies only when it says the row was
 * delivered more than once (`attempts >= 2`) — a normal single delivery is not history, and
 * printing "1 attempt" on every task would be noise dressed as disclosure.
 */
export function hasAttemptHistory(
  row: { attempts?: unknown; last_attempt_error?: unknown } | null | undefined,
): boolean {
  if (retiredAttemptError(row) !== null) return true;
  const attempts = attemptCount(row?.attempts);
  return attempts !== null && attempts >= 2;
}

/** The counter as the screen prints it: `null` when the wire carried no usable number. */
export function attemptsText(value: unknown): string | null {
  const count = attemptCount(value);
  if (count === null) return null;
  if (count === 0) return "0 attempts — no delivery recorded";
  if (count === 1) return "1 attempt";
  return `${count} attempts`;
}

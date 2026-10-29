import { ApiError } from "@/lib/api";

/**
 * The one status→chip map, shared by the tasks board, the detail sheet and the agents page
 * (agents-page.md §7: "reuse the chip for the run-status token here so the vocabulary cannot
 * drift between the two surfaces"). Same idiom as the shipped dashboard `TasksPreview`: a soft
 * chip whose label carries an 800-step hue (≥ 4.5:1 on the chip fill); a null or unknown word
 * falls back to the muted gray — never an empty chip, never another status's colour.
 *
 * The task vocabulary is the backend's `task_service.VALID_TASK_STATUSES` — including
 * `awaiting_approval` and `rejected`, which are OURS (D1/D2) and must render honestly rather
 * than being flattened into a friendlier word.
 *
 * `running` is a RUN status (`AgentStatusResponse.last_run_status`), not a task status. It is
 * mapped here so the agents page reads legibly, with a colour only — never a pulse or any
 * present-tense animation (Q-21: a historical row must not look live).
 */
const STATUS_TEXT: Record<string, string> = {
  completed: "text-green-800",
  in_progress: "text-blue-800",
  pending: "text-amber-800",
  failed: "text-red-800",
  cancelled: "text-neutral-600",
  awaiting_approval: "text-orange-800",
  rejected: "text-rose-800",
  running: "text-blue-800",
};

/** Extra classes for a `Chip variant="soft"`; the fallback keeps a null/unknown word legible. */
export function statusChipClass(status: string | null | undefined): string {
  return (status ? STATUS_TEXT[status] : undefined) ?? "text-ink-muted";
}

/**
 * The raw wire word, unchanged: the live sheet title-cases values, but per tasks.md OPEN
 * DECISION #1 the data is preserved exactly and only styled (the chip/uppercase treatment is
 * CSS). A null status is the word `unknown`, never a blank element.
 */
export function statusLabel(status: string | null | undefined): string {
  return status ?? "unknown";
}

/**
 * What the UI shows for a failure: `ApiError.display` (code + message + the server's
 * `how_to_fix`) for a named refusal; anything that never reached the server shows its own
 * message. The text is never rewritten into a friendlier sentence — Q-03/Q-04 exist precisely
 * because the transport already names the real cause (timeout vs cancellation).
 */
export function errorText(e: unknown): string {
  if (e instanceof ApiError) return e.display;
  return e instanceof Error ? e.message : String(e);
}

/**
 * F-07 — the run's OWN verdict (wave-2 backend, wave-3 UI), read from the two additive wire
 * fields `execution_status` / `needs_attention` (`backend/app/schemas/task.py:127-142`).
 *
 * The defect this exists for: tasks 311/312/314 carried `status: completed` while their own
 * envelopes said `[partial]` — every badge read COMPLETED, which was true about the lifecycle and
 * false about the outcome. The lifecycle vocabulary is FROZEN (`TASK_STATUS_VALUES`, the server's
 * `TASK_TRANSITIONS` matrix); the verdict is a SEPARATE chip that sits beside it.
 *
 * Rules:
 *   * `execution_status` is shown verbatim (uppercased) even when the word is unknown: an
 *     unrecognised verdict is the server's, not an excuse to hide it or invent a friendlier one.
 *   * `needs_attention: true` with NO execution_status still renders — the server flagged the row
 *     and named no reason; the chip says exactly that (never a silent clean success).
 *   * null/absent/false on both renders NOTHING: an honest clean completion gets no badge, so the
 *     chip's presence always means something.
 */
const VERDICT_TONE: Record<string, string> = {
  partial: "text-amber-800",
  refused: "text-rose-800",
};

export type ExecutionVerdict = {
  /** The chip label — the server's own word, uppercased. */
  label: string;
  /** The chip's text colour class (≥ 4.5:1 on the soft chip fill, like `STATUS_TEXT`). */
  tone: string;
  /** One sentence the title/description explains — what the badge does and does not mean. */
  explanation: string;
};

export function executionVerdict(row: {
  execution_status?: string | null;
  needs_attention?: boolean | null;
}): ExecutionVerdict | null {
  const raw =
    typeof row.execution_status === "string" ? row.execution_status.trim() : "";
  if (raw !== "") {
    const known = raw.toLowerCase();
    if (known === "partial") {
      return {
        label: "PARTIAL",
        tone: VERDICT_TONE.partial,
        explanation:
          "The run's own envelope said PARTIAL: it did real work but reported the result as " +
          "incomplete. The lifecycle status beside it is the server's — this was not a clean " +
          "completion.",
      };
    }
    if (known === "refused") {
      return {
        label: "REFUSED",
        tone: VERDICT_TONE.refused,
        explanation:
          "The run's own envelope said REFUSED: it produced a refusal rather than the work " +
          "asked for. The lifecycle status beside it is the server's.",
      };
    }
    return {
      label: raw.toUpperCase(),
      tone: VERDICT_TONE.partial,
      explanation:
        `The run recorded execution status "${raw}". This surface does not know that word, ` +
        "so it is shown verbatim rather than mapped to a friendlier one.",
    };
  }
  if (row.needs_attention === true) {
    return {
      label: "NEEDS ATTENTION",
      tone: VERDICT_TONE.partial,
      explanation:
        "The server flagged this row as needing attention but recorded no execution status — " +
        "the flag is shown, the reason is not invented.",
    };
  }
  return null;
}

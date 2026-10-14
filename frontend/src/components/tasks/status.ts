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

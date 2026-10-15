/**
 * The support-inbox status vocabulary — DELIBERATELY separate from `components/tasks/status.ts`:
 * the tasks map is the task status set (`task_service.VALID_TASK_STATUSES`) and the inbox runs
 * on `support_inbox_service.VALID_STATUSES = ("pending", "dispatched")` — a different vocabulary
 * that happens to share one word. Reusing the task chip map would silently mislabel `dispatched`
 * (no entry → muted fallback) and blur two contracts; this map keeps each state its own hue and
 * an unknown/null word on the same muted fallback the house chips use.
 */
const STATUS_TEXT: Record<string, string> = {
  pending: "text-amber-800",
  dispatched: "text-blue-800",
};

/** Extra classes for a `Chip variant="soft"`; null/unknown keeps the legible muted fallback. */
export function inboxStatusChipClass(status: string | null | undefined): string {
  return (status ? STATUS_TEXT[status] : undefined) ?? "text-ink-muted";
}

/** The raw wire word, unchanged; a null status is the word `unknown`, never a blank element. */
export function inboxStatusLabel(status: string | null | undefined): string {
  return status ?? "unknown";
}

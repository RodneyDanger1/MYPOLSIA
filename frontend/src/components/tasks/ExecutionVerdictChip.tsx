import { Chip } from "@/components/ui";

import { executionVerdict } from "./status";
import type { TaskRow } from "./types";

/**
 * F-07 — the chip that stops a `completed` row from rendering as a clean success.
 *
 * Wave 2's backend half serves the run's own verdict BESIDE the frozen lifecycle status
 * (`execution_status` / `needs_attention`, `backend/app/schemas/task.py:127-142`); this is the
 * screen half. It renders NOTHING when both fields are null/false, so the chip's presence always
 * carries a fact: the run said `partial`/`refused` (or the server flagged attention without a
 * word). It never rewrites `status`, which stays the server's lifecycle token on every surface.
 *
 * Presentational and hook-free on purpose: the tasks board, the detail sheet and the dashboard
 * preview all render the same component, so the three cannot drift into different vocabularies.
 */
export function ExecutionVerdictChip({
  task,
  testId,
}: {
  task: TaskRow;
  /** A stable test/query hook per surface (`task-verdict-7`, `task-detail-verdict`, …). */
  testId?: string;
}) {
  const verdict = executionVerdict(task);
  if (verdict === null) return null;
  return (
    <Chip
      variant="soft"
      className={verdict.tone}
      title={verdict.explanation}
      data-testid={testId}
    >
      {verdict.label}
    </Chip>
  );
}

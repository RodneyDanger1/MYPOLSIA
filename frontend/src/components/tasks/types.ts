import type { Task } from "@/lib/api";

/**
 * The wire row these pages render. `GET /tasks` answers with the backend's `TaskResponse`
 * (`backend/app/schemas/task.py`), which carries fields the shared `Task` type stops short of;
 * declaring them here as OPTIONAL additions is additive (no shared file is changed) — the same
 * pattern the dashboard's `TasksPreview` uses for `description`.
 *
 * Every optional field is coded defensively on the consumer side: null/absent means "not
 * recorded" and renders as the honest absence or a stated reason — never as a zero, a fabricated
 * date, or a formatted money value. (tasks.md acceptance #3's no-price rule was written when
 * `TaskResponse` had NO per-task cost field — its GAP-NO-DATA premise died with wave U2, which
 * added the measured `cost_usd`; wave U3 / B4 therefore surfaces it on the detail sheet ONLY as
 * the measured ledger value, with null kept visibly distinct from a measured zero.)
 *
 * The four U2 truth fields (all nullable, default None server-side):
 *   * `completed_at` — set only when `status == "completed"`; a failed task did not complete;
 *   * `cost_usd` — the SUM of the task's runs-ledger costs; null = unmeasured, never zero;
 *   * `tokens_in` / `tokens_out` — the same NULL discipline as `cost_usd`.
 */
export type TaskRow = Task & {
  description?: string | null;
  source?: string | null;
  result_summary?: string | null;
  error_message?: string | null;
  completed_at?: string | null;
  cost_usd?: number | null;
  tokens_in?: number | null;
  tokens_out?: number | null;
};

/** `POST /agents/{agent_type}/trigger` (agents.py:54-58, 202). */
export type AgentTriggerResponse = {
  status: string;
  task_id: number;
  message: string;
};

/**
 * `POST /approvals` (approvals.py:103-140, 201). `token` is present on the wire but is
 * deliberately never rendered — the sheet reports the mint and its expiry, not the secret.
 * `expires_at` is integer epoch SECONDS (`int(minted.expires_at)` server-side).
 */
export type ApprovalIssueResponse = {
  token: string;
  action_fingerprint: string;
  expires_at: number;
  ttl_s: number;
};

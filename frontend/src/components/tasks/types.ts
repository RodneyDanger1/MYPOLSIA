import type { Task } from "@/lib/api";

/**
 * The plan edge the orchestrator persisted on the task row (`TaskResponse.plan`, F-36/D1).
 *
 * Provenance, not a claim: the keys the morning cycle persists on `tasks.metadata`
 * (`celery_app/tasks/daily_cycle.py:619-628` first pass, `:673-682` the resolved ids) and what
 * `TaskResponse.plan` mirrors WITHOUT renaming (`backend/app/schemas/task.py`
 * `PLAN_METADATA_KEYS`). It is NOT the raw `metadata` bag — that holds the private
 * `approval_token`, which is why the server computes this shape instead of serving the dict.
 *
 * THE SPELLINGS MATTER (measured against D1's computed field):
 *   - `depends_on` is the RESOLVED DAG edge: task IDS (integers), not names. A renderer that
 *     joins it as text prints "42, 43" where the operator needs titles.
 *   - `depends_on_titles` is the plan's own dependency titles (strings, capped at 8);
 *   - `depends_on_unresolved` names the titles the planner could not match to a planned task;
 *   - `depends_on_cycle` says the plan was not a DAG and this row was released as a root with its
 *     edges DROPPED (`daily_cycle.py:700-705`), so presenting them as live would lie. It carries
 *     the MEMBER TITLES when the server serves the list the producer recorded; see the field.
 *
 * Every key is optional: the server omits an estimate the model never stated, and absence prints
 * as absence — "the model did not price it" and "it costs nothing" are different facts.
 * `depends_on` also accepts strings for the PRE-D1 shape the A3 renderer was written against (a
 * title list under the same key), so an older row still renders its real names rather than ids.
 */
export type PlanEdge = {
  depends_on?: number[] | string[] | null;
  depends_on_titles?: (string | null)[] | null;
  depends_on_unresolved?: (string | null)[] | null;
  /**
   * The cycle that stranded this task — the TITLES of the edges it was left waiting on.
   *
   * TWO spellings, and the difference is a fact, not a style: the morning cycle records the LIST
   * (`daily_cycle.py:700-705` writes `[titles_by_id[dep] for dep in edges[node]]`), while D1's
   * computed `TaskResponse.plan` flattens it — `isinstance(cycle, list) and bool(cycle)`
   * (`backend/app/schemas/task.py:142`). So a server can answer `true` (a cycle was recorded, and
   * its members were NOT served) or the member list itself. `planCycleMembers` /
   * `planCycleDropped` below read both; nothing here treats `true` as if it carried names, and
   * the sheet says which of the two facts it actually has.
   */
  depends_on_cycle?: boolean | string[] | null;
  plan_day?: string | null;
  side_effect?: string | null;
  estimate_usd?: number | null;
  estimate_seconds?: number | null;
  acceptance?: string | null;
};

/** The dependency TITLES a plan edge records, in either spelling the server may serve: the
 *  plan's own `depends_on_titles` first, then a string-valued `depends_on` (the pre-D1 shape). */
export function planDependencies(plan: PlanEdge): string[] {
  const strings = (values: unknown): string[] =>
    Array.isArray(values)
      ? values
          .map((value) => (typeof value === "string" ? value.trim() : ""))
          .filter((value) => value.length > 0)
      : [];

  const titles = strings(plan.depends_on_titles);
  if (titles.length > 0) return titles;
  const legacy = strings(plan.depends_on);
  if (legacy.length > 0) return legacy;
  return [];
}

/** The RESOLVED dependency edge — task ids the server actually recorded (`depends_on_ids`).
 *  Rendered only as ids, and only when no titles exist: an id is not a name. */
export function planDependencyIds(plan: PlanEdge): number[] {
  if (!Array.isArray(plan.depends_on)) return [];
  return plan.depends_on.filter(
    (value): value is number => typeof value === "number" && Number.isInteger(value),
  );
}

/** The dependency titles the planner could not match to a planned task (`depends_on_unresolved`). */
export function planUnresolvedDependencies(plan: PlanEdge): string[] {
  if (!Array.isArray(plan.depends_on_unresolved)) return [];
  return plan.depends_on_unresolved
    .map((value) => (typeof value === "string" ? value.trim() : ""))
    .filter((value) => value.length > 0);
}

/** The member TITLES of the cycle that stranded this task, in the order the plan recorded them.
 *
 *  An older server's flattened `true` answers "a cycle was recorded" and carries NO names: that
 *  is an empty list here, never an invented one. A non-string entry is dropped rather than
 *  stringified — `String(null)` would print "null" as a task title. */
export function planCycleMembers(plan: PlanEdge): string[] {
  const cycle = plan.depends_on_cycle;
  if (!Array.isArray(cycle)) return [];
  return cycle
    .map((value) => (typeof value === "string" ? value.trim() : ""))
    .filter((value) => value.length > 0);
}

/** True when the plan recorded that this node was released as a root because its edge set was a
 *  cycle — in EITHER spelling: the pre-flattening list (non-empty), or D1's `true`. An empty list
 *  is not a cycle (the producer only writes the key for a stranded node). */
export function planCycleDropped(plan: PlanEdge): boolean {
  if (Array.isArray(plan.depends_on_cycle)) return planCycleMembers(plan).length > 0;
  return plan.depends_on_cycle === true;
}

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
 *
 * The two F-36 / A5-02 computed fields:
 *   * `plan` — the plan edge (above); null when the task was not materialised by the orchestrator,
 *     so a hand-made task renders no provenance section at all;
 *   * `approval_required` / `approval_fingerprints` — D1's parked-approval pair (below). The ONE
 *     reader of both is `approval.ts`; no surface reads either field directly.
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
  plan?: PlanEdge | null;
  /**
   * D1's parked-approval FLAG — a BOOLEAN (`bool(approval_fingerprints)`,
   * `backend/app/schemas/task.py:152-162`), derived from the list beside it.
   *
   * The pre-D1 spelling put the list itself here (the A3 pins were written against it, and an
   * older server still serves it), so both spellings are declared. `Array.isArray(true)` is
   * FALSE: a consumer that reads this field as the fingerprint list silently disarms the Approve
   * control on exactly the rows the server marks as parked — the measured hazard this wave fixes.
   * Read it through `approval.ts`, never directly.
   */
  approval_required?: boolean | { fingerprint: string }[] | null;
  /**
   * D1's fingerprints (`task.py:164-182`), de-duplicated in order, narrowed to the one value the
   * mint endpoint accepts. PRESENT means the server ANSWERED: `[]` is "nothing is parked", not
   * "unknown", so an empty list is never backfilled from `result_summary` prose.
   */
  approval_fingerprints?: string[] | null;
};

/** `POST /agents/{agent_type}/trigger` (agents.py:54-58, 202). */
export type AgentTriggerResponse = {
  status: string;
  task_id: number;
  message: string;
};

/**
 * `GET/POST /tasks/{task_id}/attachments` (V25/L1). Metadata only: the bytes live behind
 * `GET /api/v1/attachments/{id}` and are fetched as a blob for the thumbnail (an `<img>` tag
 * cannot send the owner key). `mime` is the SNIFFED type the server recorded at upload.
 */
export type TaskAttachment = {
  id: number;
  task_id: number;
  filename: string;
  mime: string;
  size_bytes: number;
  sha256: string;
  created_at?: string | null;
};

/**
 * `POST /tasks/{task_id}/run` (V24/L1, backend tasks.py — 202). This is the verb that runs the
 * EXISTING row: it does not create a task. `status` is the literal `"dispatched"` (one message
 * accepted by the broker) — it is NOT the task's status, which only the worker's claim moves.
 */
export type TaskRunDispatchResponse = {
  task_id: number;
  status: string;
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

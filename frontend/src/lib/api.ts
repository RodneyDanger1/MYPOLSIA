const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost";
export const API_KEY = process.env.NEXT_PUBLIC_API_KEY ?? "";

const DEFAULT_TIMEOUT_MS = 15_000;
// Retry only idempotent reads, and only on transport or 5xx failures. A POST is never retried
// blindly: the server's idempotency keys make a retry safe, but a blind client retry would double
// the load on a backend that is already struggling.
const RETRY_ATTEMPTS = 2;
const RETRY_BASE_MS = 300;
/** Q-04: the WHOLE read call — every attempt AND every backoff sleep — fits this budget. A slow
 *  backend returning late 5xx used to burn ≈46 s (3 × 15 s attempts + 900 ms of backoff) before any
 *  error reached the UI; with the default timeout the real worst case is now ≈20 s, and the error
 *  that finally surfaces is a real one. A caller that passes its own (larger) `timeoutMs` — e.g. the
 *  memory page's deliberately warm 120 s search — explicitly buys a matching total budget; its
 *  retries still cannot outlive that. */
export const READ_TOTAL_BUDGET_MS = 20_000;
/** A retry is only worth scheduling if at least this much of the budget survives the backoff sleep,
 *  so the retry gets a usable attempt window instead of an instant abort. */
const MIN_RETRY_BUDGET_MS = 1_000;

/** A failure the API described itself, in the server's named-refusal shape. */
export type ApiRefusal = {
  code: string;
  message: string;
  how_to_fix?: string;
  context?: Record<string, unknown>;
};

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly howToFix: string;
  readonly body: unknown;
  /** The server's correlation id for THIS call — the `X-Request-ID` response header `main.py`'s
   *  middleware sets on every response and the CORS layer exposes cross-origin. Absent for
   *  transport-level failures that never reached the server (and for responses that carry none).
   *  `error.tsx` shows it so the user does not have to hunt for the id. */
  readonly requestId?: string;

  constructor(
    status: number,
    message: string,
    code = "E_HTTP",
    howToFix = "",
    body?: unknown,
    requestId?: string,
  ) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.howToFix = howToFix;
    this.body = body;
    this.requestId = requestId;
  }

  /** What the UI should show: the code and, when the server supplied one, the fix hint. */
  get display(): string {
    return this.howToFix
      ? `${this.code}: ${this.message} — ${this.howToFix}`
      : `${this.code}: ${this.message}`;
  }
}

/** The named refusal carried by a response, in EITHER wire shape the backend emits (D5; audit L5-01).
 *
 *  The app's PolsiaError handler emits the envelope `{ok: false, error: {code, message, how_to_fix}}`;
 *  FastAPI's default handler serializes `HTTPException(detail={code, ...})` — the shape `refusal_guard`
 *  and direct raises produce — as `{"detail": {code, ...}}`. Before this, only the envelope was parsed,
 *  so every guard-path refusal degraded to `E_HTTP_<status>` with a raw JSON blob as its message. A
 *  `detail` payload WITHOUT a `code` (plain 422s, incidental HTTPExceptions) stays a generic `E_HTTP_*`
 *  — those are not refusals and must not pretend to be.
 */
function refusalFrom(body: unknown): ApiRefusal | null {
  if (!body || typeof body !== "object") return null;
  const b = body as { ok?: unknown; error?: unknown; detail?: unknown };
  if (b.ok === false && b.error !== null && typeof b.error === "object") {
    const e = b.error as Partial<ApiRefusal>;
    if (typeof e.code === "string" && typeof e.message === "string") {
      return { code: e.code, message: e.message, how_to_fix: e.how_to_fix ?? "", context: e.context };
    }
    return null;
  }
  if (b.detail !== null && typeof b.detail === "object") {
    const d = b.detail as Partial<ApiRefusal>;
    if (typeof d.code === "string") {
      return {
        code: d.code,
        message: typeof d.message === "string" ? d.message : d.code,
        how_to_fix: typeof d.how_to_fix === "string" ? d.how_to_fix : "",
        context: d.context,
      };
    }
  }
  return null;
}

function sleep(ms: number) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

/** Per-call transport overrides. `timeoutMs`/`retries`/`budgetMs` are read off the same object that
 *  is spread into `RequestInit`, so pages may pass them through `api.get`'s second argument. */
export type ApiFetchOptions = RequestInit & {
  /** Per-attempt timeout (default 15 000 ms). */
  timeoutMs?: number;
  /** Retry count override for idempotent reads (default RETRY_ATTEMPTS = 2). */
  retries?: number;
  /** Whole-call budget override for idempotent reads (default READ_TOTAL_BUDGET_MS = 20 000 ms). */
  budgetMs?: number;
};

/** A retry is worth it only if the backoff plus a usable attempt window still fit the budget. */
function canRetry(attempt: number, maxRetries: number, deadline: number): boolean {
  if (attempt >= maxRetries) return false;
  const backoff = RETRY_BASE_MS * 2 ** attempt;
  return Date.now() + backoff + MIN_RETRY_BUDGET_MS <= deadline;
}

async function apiFetch<T>(path: string, options: ApiFetchOptions = {}): Promise<T> {
  const { timeoutMs = DEFAULT_TIMEOUT_MS, retries, budgetMs, ...init } = options;
  const method = (init.method ?? "GET").toUpperCase();
  const retryable = method === "GET" || method === "HEAD";
  const maxRetries = retryable ? (retries ?? RETRY_ATTEMPTS) : 0;
  // Bound the WHOLE read call, not just each attempt (Q-04). An explicit caller `timeoutMs` larger
  // than the budget raises it to that intent (the memory page's 120 s warm search), while a default
  // read can never outlive READ_TOTAL_BUDGET_MS. Mutations keep their single bounded attempt.
  const deadline = retryable
    ? Date.now() + Math.max(budgetMs ?? READ_TOTAL_BUDGET_MS, timeoutMs)
    : Number.POSITIVE_INFINITY;

  let attempt = 0; // retries consumed so far
  // Every exit from this loop is an explicit return/throw — there is no unreachable code after it
  // (audit Q-05; the old trailing `throw lastError` could never run).
  for (;;) {
    const remaining = deadline - Date.now();
    if (retryable && remaining <= 0) {
      // Reachable only when a backoff sleep (or a busy event loop) overshot the budget between the
      // retry gate and here; an explicit, honest terminal state rather than a silent fall-through.
      throw new ApiError(
        0,
        `read budget of ${budgetMs ?? READ_TOTAL_BUDGET_MS}ms exhausted after ${attempt} ${
          attempt === 1 ? "retry" : "retries"
        }`,
        "E_TIMEOUT",
      );
    }
    const attemptTimeout = retryable ? Math.min(timeoutMs, remaining) : timeoutMs;

    const controller = new AbortController();
    // Q-03: TWO different causes abort this ONE controller — our timeout and the consumer's signal
    // (component unmount). Track which one fired, or an unmount-abort gets reported to the user as
    // "request timed out".
    let timedOut = false;
    const timer = setTimeout(() => {
      timedOut = true;
      controller.abort();
    }, attemptTimeout);
    const onConsumerAbort = () => controller.abort();
    if (init.signal) {
      // Honour an outer signal (component unmount) as well as our own timeout.
      if (init.signal.aborted) controller.abort();
      else init.signal.addEventListener("abort", onConsumerAbort, { once: true });
    }

    try {
      const res = await fetch(`${API_URL}/api/v1${path}`, {
        ...init,
        signal: controller.signal,
        headers: {
          "Content-Type": "application/json",
          // Only send a credential when there IS one. An empty `X-API-Key` is not a credential, and a
          // backend that is open (no API_KEY configured) rejects a blank value where it accepts no
          // header at all — which locked the whole UI out until the e2e gate caught it.
          ...(API_KEY ? { "X-API-Key": API_KEY } : {}),
          ...init.headers,
        },
      });

      if (!res.ok) {
        // The server's correlation id for THIS call (fix-5 §1 / T15c): read it BEFORE consuming the
        // body, so even a bodiless refusal can carry it onto the error the UI renders.
        const requestId = res.headers.get("X-Request-ID") ?? undefined;
        const text = await res.text().catch(() => res.statusText);
        let parsed: unknown;
        try {
          parsed = JSON.parse(text);
        } catch {
          parsed = undefined;
        }
        const refusal = refusalFrom(parsed);
        if (refusal) {
          throw new ApiError(
            res.status,
            refusal.message,
            refusal.code,
            refusal.how_to_fix ?? "",
            parsed,
            requestId,
          );
        }
        const err = new ApiError(
          res.status,
          text || res.statusText,
          `E_HTTP_${res.status}`,
          "",
          undefined,
          requestId,
        );
        // A 5xx on a read is worth one more try, if the budget still fits it; a 4xx never is.
        if (res.status >= 500 && canRetry(attempt, maxRetries, deadline)) {
          await sleep(RETRY_BASE_MS * 2 ** attempt);
          attempt += 1;
          continue;
        }
        throw err;
      }

      if (res.status === 204) return undefined as T;
      return (await res.json()) as T;
    } catch (e) {
      if (e instanceof ApiError) throw e;
      const aborted = (e as { name?: string })?.name === "AbortError";
      if (aborted) {
        // Q-03: name the REAL cause — a timeout is a timeout, a consumer abort is a cancellation.
        throw timedOut
          ? new ApiError(0, `no response within ${attemptTimeout}ms`, "E_TIMEOUT")
          : new ApiError(0, "request cancelled", "E_ABORTED");
      }
      if (canRetry(attempt, maxRetries, deadline)) {
        await sleep(RETRY_BASE_MS * 2 ** attempt);
        attempt += 1;
        continue;
      }
      throw new ApiError(0, String(e), "E_NETWORK");
    } finally {
      clearTimeout(timer);
      init.signal?.removeEventListener("abort", onConsumerAbort);
    }
  }
}

export const api = {
  get: <T>(path: string, init?: ApiFetchOptions) => apiFetch<T>(path, { ...init, method: "GET" }),
  post: <T>(path: string, body?: unknown, init?: ApiFetchOptions) =>
    apiFetch<T>(path, { ...init, method: "POST", body: JSON.stringify(body ?? {}) }),
  put: <T>(path: string, body?: unknown, init?: ApiFetchOptions) =>
    apiFetch<T>(path, { ...init, method: "PUT", body: JSON.stringify(body ?? {}) }),
  /** V11/W3 — the frozen interface of UNIT2-PLAN: a verbatim mirror of `put`, method "PATCH".
   *  Added FIRST (before the invoice/schedule pages) because W5's task-status control consumes it.
   *  Covered by `src/__tests__/u11-schedules.test.tsx` ("api.patch — the REAL client method"). */
  patch: <T>(path: string, body?: unknown, init?: ApiFetchOptions) =>
    apiFetch<T>(path, { ...init, method: "PATCH", body: JSON.stringify(body ?? {}) }),
  delete: <T>(path: string, init?: ApiFetchOptions) =>
    apiFetch<T>(path, { ...init, method: "DELETE" }),
};

/** GET/PUT/DELETE /config/deepseek-key (RD9). Masked status only — the raw key has no
 *  representation in this type because the API never returns it; `masked` is a display
 *  form like `sk-…1234`, and `source` says where the live key comes from. */
export type DeepSeekKeyStatus = {
  configured: boolean;
  source: "stored" | "env" | "none";
  masked: string;
};

/** POST /config/deepseek-key/verify — the outcome of a REAL `GET https://api.deepseek.com/models`
 *  probe. `ok: false` carries the genuine reason (HTTP status, timeout, transport error); the UI
 *  must show `detail` as-is rather than inventing a friendlier verdict. */
export type DeepSeekVerifyResult = {
  ok: boolean;
  detail: string;
};

export type ActivityEvent = {
  id: number;
  agent_type: string;
  action: string;
  summary: string;
  /** An open string, not a closed union. The server types the field `level: str`
   *  (`ActivityEventResponse` in `app/schemas/report.py`) and only normalises NULL -> "info", so
   *  "info"/"success"/"warning"/"error" are what today's writers emit, not what the contract
   *  promises. The feed renders an unknown word with its gray fallback (`ActivityFeed.tsx`) instead
   *  of treating it as impossible. */
  level: string;
  /** `datetime | None` server-side (`ActivityEventResponse.created_at`; the column is nullable with
   *  a `now()` default), so null is a real wire value: the feed renders it as "date unknown", the
   *  way the memory page does — never as `new Date(null)`, which prints 1/1/1970. */
  created_at: string | null;
};

export type DashboardSummary = {
  tasks_today_total: number;
  tasks_today_completed: number;
  tasks_today_pending: number;
  tasks_today_failed: number;
  active_agents: string[];
  kpis: Record<string, unknown>;
  last_report_date: string | null;
};

export type Task = {
  id: number;
  title: string;
  agent_type: string;
  /** `str | None` server-side (`TaskResponse.status`; the tasks column is nullable — `0001`'s
   *  `CREATE TABLE tasks` carries `server_default='pending'` but no `NOT NULL`), so a null is a
   *  real wire value: the page renders its gray "unknown" badge for it, never an empty chip. */
  status: string | null;
  /** Same nullable contract as `status` (`server_default='3'`, no `NOT NULL`) — the page shows
   *  the em dash for a null, never a fabricated `3`. */
  priority: number | null;
  /** Same nullable contract as `ActivityEvent.created_at` — `TimestampedORM.created_at` is
   *  `datetime | None` (`app/schemas/common.py`). */
  created_at: string | null;
};

export type AgentStatus = {
  agent_type: string;
  last_run_at: string | null;
  last_run_status: string | null;
  tasks_today: number;
  tasks_total: number;
};

/** One agent's row inside `ai_spend.unit_economics.per_agent` — the backend's `AgentEconomicsView`
 *  (`app/schemas/finance.py`). `cost_per_completed_task_usd` is `float | None` server-side: null is
 *  "no completion was measured", never `$0.00`. */
export type AgentEconomics = {
  spend_usd: number;
  completed: number;
  cost_per_completed_task_usd: number | null;
};

/** `ai_spend.burn_rate` — the literal output of the backend's `accounting.burn_rate`
 *  (`AiBurnRateView`). `projected_day_total_usd` is null only in the first instant of the local
 *  day, where the function refuses to project from a zero-length window. */
export type AiBurnRate = {
  spent_usd: number;
  day_limit_usd: number;
  elapsed_fraction: number;
  projected_day_total_usd: number | null;
  status: string;
};

/** `ai_spend.unit_economics` (`UnitEconomicsView`): totals over the same company-local day, plus
 *  the additive `unmeasured_spend_agents` marker — an agent whose day spend was NOT measurable is
 *  NAMED here rather than priced at a fabricated per-task cost. */
export type AiUnitEconomics = {
  total_spend_usd: number;
  total_completed: number;
  cost_per_completed_task_usd: number | null;
  per_agent: Record<string, AgentEconomics>;
  note: string;
  unmeasured_spend_agents: string[];
};

/** `ai_spend` — the additive AI-spend day view of `GET /finance/summary` (R24; backend
 *  `AiSpendSummary`). `limit` is the day cap the dispatch gate enforces; `spent`/`remaining` are
 *  `float | None` — null is "the day could not be measured" (an absent day is a measured 0.0,
 *  never the other way round), so a renderer must branch on null before formatting. */
export type AiSpend = {
  limit: number;
  spent: number | null;
  remaining: number | null;
  burn_rate: AiBurnRate | null;
  unit_economics: AiUnitEconomics | null;
};

/** GET /finance/summary. Money fields are `number | null`: null is "not measured" (no
 *  snapshot yet, nothing to aggregate) and renders as an em dash — never as 0. */
export type FinanceSummary = {
  mrr_cents: number | null;
  arr_cents: number | null;
  active_subscribers: number | null;
  total_ad_spend_usd: number | null;
  total_expenses_month_cents: number | null;
  stripe_balance_cents: number | null;
  last_snapshot_date: string | null;
  /** R24's additive block, adopted here from the page's local declaration (fix-4 §2 / T15a).
   *  Optional AND nullable for the same reason as the money fields above: an absent block is
   *  "not measured" and must render as the em dash, never as a fabricated `$0.00`. */
  ai_spend?: AiSpend | null;
};

/* ── V11/W3 — invoices (wiring audit F2) ─────────────────────────────────────────────────────── */
/** One invoice row — the wire shape of `app.schemas.invoice.InvoiceResponse`
 *  (`backend/app/schemas/invoice.py`), served by `GET /invoices` and returned by the create /
 *  send / void routes (`backend/app/api/v1/invoices.py` :64/:85/:103/:122). Money is
 *  `number | null`: the service stores an absent wire value as NULL (never a fabricated 0), so
 *  a renderer must branch on null before formatting — the em-dash law. `status` is an open
 *  string server-side (`str`), today one of draft|sent|paid|void|overdue; an unknown word
 *  renders as itself rather than being disguised. `paid_at` is `datetime | None`. */
export type Invoice = {
  id: number;
  number: string;
  bill_to: Record<string, unknown> | null;
  issue_date: string;
  due_date: string;
  net_days: number;
  currency: string;
  subtotal: number | null;
  tax: number | null;
  total: number | null;
  status: string;
  payment_link: string | null;
  paid_at: string | null;
  late_fee_note: string | null;
  line_items: Record<string, unknown>[] | null;
};

/** `POST /invoices` body — `InvoiceCreateRequest` declared in `backend/app/api/v1/invoices.py`
 *  (:33-49; the request models live in the router because the schemas package is frozen).
 *  Every field optional: `bill_to`/`line_items` are free-form JSON the service stores verbatim,
 *  absent money stays absent on the wire (`null` stays `null`, never coerced to 0). */
export type InvoiceCreate = {
  bill_to?: Record<string, unknown>;
  line_items?: Record<string, unknown>[];
  issue_date?: string;
  due_date?: string;
  net_days?: number;
  currency?: string;
  subtotal?: number;
  tax?: number;
  total?: number;
  payment_link?: string;
};

/* ── V11/W3 — schedules (wiring audit F1) ────────────────────────────────────────────────────── */
/** The frozen cadence value set of `app.schemas.schedule.Cadence` (once|daily|weekdays|weekly). */
export type ScheduleCadence = "once" | "daily" | "weekdays" | "weekly";

/** One schedule row — `app.schemas.schedule.ScheduleResponse` (`backend/app/schemas/schedule.py`),
 *  served by GET/POST/PATCH/DELETE `/schedules` (`backend/app/api/v1/schedules.py` :39/:50/:70/:84).
 *  `hour` is the COMPANY-LOCAL wall-clock hour (0-23); `weekday` (0-6, Monday=0) applies to
 *  `weekly` only and is null otherwise; `enabled=false` IS the pause (a paused row carries a
 *  cleared `next_run_at`). `next_run_at`/`last_run_at`/`created_at` are `datetime | None` —
 *  null renders as the em dash, never an invented instant. */
export type Schedule = {
  id: number;
  name: string;
  prompt: string;
  cadence: string;
  hour: number;
  weekday: number | null;
  enabled: boolean;
  next_run_at: string | null;
  last_run_at: string | null;
  created_at: string | null;
};

/** `GET /schedules` — `ScheduleListResponse`: the rows plus the server's own count. */
export type ScheduleList = { items: Schedule[]; total: number };

/** `POST /schedules` body — `ScheduleCreate` (`app/schemas/schedule.py` :21-29). `enabled`
 *  defaults FALSE server-side: a schedule is created PAUSED unless the client states otherwise. */
export type ScheduleCreate = {
  name: string;
  prompt: string;
  cadence: ScheduleCadence;
  hour: number;
  weekday?: number;
  enabled?: boolean;
};

/** `PATCH /schedules/{id}` body — `ScheduleUpdate` (:32-40); only the PROVIDED keys are applied
 *  (`exclude_unset`), so the page sends exactly the fields the user changed. Pause = `{enabled:
 *  false}`; `weekday: null` clears the column (legal only for cadences that do not use it). */
export type ScheduleUpdate = {
  name?: string;
  prompt?: string;
  cadence?: ScheduleCadence;
  hour?: number;
  weekday?: number | null;
  enabled?: boolean;
};

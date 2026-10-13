const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost";
export const API_KEY = process.env.NEXT_PUBLIC_API_KEY ?? "";

const DEFAULT_TIMEOUT_MS = 15_000;
// Retry only idempotent reads, and only on transport or 5xx failures. A POST is never retried
// blindly: the server's idempotency keys make a retry safe, but a blind client retry would double
// the load on a backend that is already struggling.
const RETRY_ATTEMPTS = 2;
const RETRY_BASE_MS = 300;

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

  constructor(status: number, message: string, code = "E_HTTP", howToFix = "", body?: unknown) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.howToFix = howToFix;
    this.body = body;
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

async function apiFetch<T>(
  path: string,
  options: RequestInit & { timeoutMs?: number } = {},
): Promise<T> {
  const { timeoutMs = DEFAULT_TIMEOUT_MS, ...init } = options;
  const method = (init.method ?? "GET").toUpperCase();
  const retryable = method === "GET" || method === "HEAD";
  let lastError: unknown;

  for (let attempt = 0; attempt <= (retryable ? RETRY_ATTEMPTS : 0); attempt++) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    if (init.signal) {
      // Honour an outer signal (component unmount) as well as our own timeout.
      if (init.signal.aborted) controller.abort();
      else init.signal.addEventListener("abort", () => controller.abort(), { once: true });
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
        const text = await res.text().catch(() => res.statusText);
        let parsed: unknown;
        try {
          parsed = JSON.parse(text);
        } catch {
          parsed = undefined;
        }
        const refusal = refusalFrom(parsed);
        if (refusal) {
          throw new ApiError(res.status, refusal.message, refusal.code, refusal.how_to_fix ?? "", parsed);
        }
        const err = new ApiError(res.status, text || res.statusText, `E_HTTP_${res.status}`);
        // A 5xx on a read is worth one more try; a 4xx never is.
        if (res.status >= 500 && retryable && attempt < RETRY_ATTEMPTS) {
          lastError = err;
          await sleep(RETRY_BASE_MS * 2 ** attempt);
          continue;
        }
        throw err;
      }

      if (res.status === 204) return undefined as T;
      return (await res.json()) as T;
    } catch (e) {
      if (e instanceof ApiError) throw e;
      lastError = e;
      const aborted = (e as { name?: string })?.name === "AbortError";
      if (retryable && !aborted && attempt < RETRY_ATTEMPTS) {
        await sleep(RETRY_BASE_MS * 2 ** attempt);
        continue;
      }
      throw new ApiError(
        0,
        aborted ? `request timed out after ${timeoutMs}ms` : String(e),
        "E_NETWORK",
      );
    } finally {
      clearTimeout(timer);
    }
  }
  throw lastError instanceof Error ? lastError : new ApiError(0, String(lastError), "E_NETWORK");
}

export const api = {
  get: <T>(path: string, init?: RequestInit) => apiFetch<T>(path, { ...init, method: "GET" }),
  post: <T>(path: string, body?: unknown, init?: RequestInit) =>
    apiFetch<T>(path, { ...init, method: "POST", body: JSON.stringify(body ?? {}) }),
  put: <T>(path: string, body?: unknown, init?: RequestInit) =>
    apiFetch<T>(path, { ...init, method: "PUT", body: JSON.stringify(body ?? {}) }),
  delete: <T>(path: string, init?: RequestInit) =>
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
};

const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost";
const API_KEY = process.env.NEXT_PUBLIC_API_KEY ?? "";

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

function isRefusal(body: unknown): body is { ok: false; error: ApiRefusal } {
  return (
    !!body &&
    typeof body === "object" &&
    (body as { ok?: unknown }).ok === false &&
    typeof (body as { error?: unknown }).error === "object" &&
    (body as { error: ApiRefusal }).error !== null
  );
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
        if (isRefusal(parsed)) {
          const { code, message, how_to_fix } = parsed.error;
          throw new ApiError(res.status, message, code, how_to_fix ?? "", parsed);
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
};

export type ActivityEvent = {
  id: number;
  agent_type: string;
  action: string;
  summary: string;
  level: "info" | "success" | "warning" | "error";
  created_at: string;
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
  status: string;
  priority: number;
  created_at: string;
};

export type AgentStatus = {
  agent_type: string;
  last_run_at: string | null;
  last_run_status: string | null;
  tasks_today: number;
  tasks_total: number;
};

export type FinanceSummary = {
  mrr_cents: number;
  arr_cents: number;
  active_subscribers: number;
  total_ad_spend_usd: number;
  total_expenses_month_cents: number;
  stripe_balance_cents: number;
  last_snapshot_date: string | null;
};

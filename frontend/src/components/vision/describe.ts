import { API_KEY, API_URL, ApiError } from "@/lib/api";

/**
 * V26 / L4 — the client half of vision on demand: `POST /api/v1/vision/describe`.
 * V27 / L4 — plus the task's description history: `GET /api/v1/vision/descriptions?task_id=`.
 *
 * One attachment in, the model's own sentence out:
 *
 *   request  `{"attachment_id": 11}`                       (or `{"task_id": 7}` for a task's
 *                                                           single image — the UI always names
 *                                                           the attachment it rendered)
 *   response `{"description": "...", "model": "deepseek-flash", "tokens": 138, "cached": false}`
 *            `tokens` is `null` when the provider did not measure usage — render "unmeasured",
 *            never a fabricated 0.  `cached: true` means the sentence came from the backend's
 *            `vision_descriptions` memory and NO provider call was made for this request.
 *
 * A refusal arrives in the repo envelope (`{ok:false, error:{code, message, how_to_fix}}`) and
 * is raised as the SAME `ApiError` the rest of the task surface renders (`readErrorText` ->
 * `ApiError.display`), so `E_CONFIG` / `E_UPSTREAM` / `E_NOT_FOUND` are shown exactly as the
 * server named them.
 *
 * Why a local `request` instead of reusing the one in `components/tasks/attachments.ts`: that
 * helper is module-private and that file is outside this lane's write set.  The parsing here is
 * deliberately identical (`ok:false.error` and FastAPI's coded `detail` both accepted).
 */

export type VisionDescription = {
  description: string;
  model: string;
  /** Measured prompt+completion tokens, or null when the provider did not report them. */
  tokens: number | null;
  /** True when the backend served this sentence from memory with no provider call. */
  cached: boolean;
};

/** One row of `GET /api/v1/vision/descriptions?task_id=` (newest first on the wire). */
export type VisionHistoryRow = {
  id: number;
  attachment_id: number;
  /** Leading 12 hex characters of the image digest — a display handle, not the full key. */
  sha256_prefix: string;
  description: string;
  model: string | null;
  tokens: number | null;
  created_at: string | null;
};

function refusalFrom(body: unknown): { code: string; message: string; how_to_fix?: string } | null {
  if (!body || typeof body !== "object") return null;
  const b = body as { ok?: unknown; error?: unknown; detail?: unknown };
  const raw =
    b.ok === false && b.error && typeof b.error === "object"
      ? b.error
      : b.detail && typeof b.detail === "object"
        ? b.detail
        : null;
  if (!raw) return null;
  const r = raw as { code?: unknown; message?: unknown; how_to_fix?: unknown };
  if (typeof r.code !== "string" || typeof r.message !== "string") return null;
  return {
    code: r.code,
    message: r.message,
    how_to_fix: typeof r.how_to_fix === "string" ? r.how_to_fix : "",
  };
}

/** `POST /api/v1/vision/describe` for ONE attachment.  Refuses exactly as the server does. */
export async function describeAttachment(attachmentId: number): Promise<VisionDescription> {
  const res = await fetch(`${API_URL}/api/v1/vision/describe`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      ...(API_KEY ? { "X-API-Key": API_KEY } : {}),
    },
    body: JSON.stringify({ attachment_id: attachmentId }),
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
    const requestId = res.headers.get("X-Request-ID") ?? undefined;
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
    throw new ApiError(res.status, text || res.statusText, `E_HTTP_${res.status}`, "", undefined, requestId);
  }

  const body = (await res.json()) as Partial<VisionDescription>;
  if (typeof body.description !== "string" || body.description.trim() === "") {
    // A 200 with no sentence is not a description; refuse rather than render an empty block.
    throw new ApiError(
      res.status,
      "The server answered without a description — nothing is shown for this image.",
      "E_UPSTREAM",
    );
  }
  return {
    description: body.description,
    model: typeof body.model === "string" && body.model !== "" ? body.model : "unknown model",
    tokens:
      typeof body.tokens === "number" && Number.isFinite(body.tokens) && body.tokens >= 0
        ? body.tokens
        : null,
    // Only the literal `true` is a cache hit; a missing/unknown value means "this call measured
    // it" (the honest default — never claim a provider call was skipped without evidence).
    cached: body.cached === true,
  };
}

/**
 * `GET /api/v1/vision/descriptions?task_id=<id>` — this task's stored descriptions, newest first.
 *
 * The server answers a bare array.  A row with no sentence is not filtered out here: the
 * backend cannot store one (it refuses an empty description), and silently dropping a row the
 * server sent would make the history disagree with the table.  Refusals are raised as the same
 * `ApiError` the describe call raises, so the block can render the server's own code.
 */
export async function listVisionHistory(taskId: number): Promise<VisionHistoryRow[]> {
  const res = await fetch(
    `${API_URL}/api/v1/vision/descriptions?task_id=${encodeURIComponent(String(taskId))}`,
    {
      method: "GET",
      headers: {
        ...(API_KEY ? { "X-API-Key": API_KEY } : {}),
      },
    },
  );

  if (!res.ok) {
    const text = await res.text().catch(() => res.statusText);
    let parsed: unknown;
    try {
      parsed = JSON.parse(text);
    } catch {
      parsed = undefined;
    }
    const refusal = refusalFrom(parsed);
    const requestId = res.headers.get("X-Request-ID") ?? undefined;
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
    throw new ApiError(res.status, text || res.statusText, `E_HTTP_${res.status}`, "", undefined, requestId);
  }

  const body = (await res.json()) as unknown;
  if (!Array.isArray(body)) {
    throw new ApiError(
      res.status,
      "The server answered the history request with something that is not a list.",
      "E_UPSTREAM",
    );
  }
  return body.map((raw): VisionHistoryRow => {
    const row = (raw ?? {}) as Record<string, unknown>;
    return {
      id: typeof row.id === "number" ? row.id : 0,
      attachment_id: typeof row.attachment_id === "number" ? row.attachment_id : 0,
      sha256_prefix: typeof row.sha256_prefix === "string" ? row.sha256_prefix : "",
      description: typeof row.description === "string" ? row.description : "",
      model: typeof row.model === "string" && row.model !== "" ? row.model : null,
      tokens:
        typeof row.tokens === "number" && Number.isFinite(row.tokens) && row.tokens >= 0
          ? row.tokens
          : null,
      created_at: typeof row.created_at === "string" ? row.created_at : null,
    };
  });
}

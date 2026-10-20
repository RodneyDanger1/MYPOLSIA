import { API_KEY, API_URL, ApiError } from "@/lib/api";

/**
 * V26 / L4 — the client half of vision on demand: `POST /api/v1/vision/describe`.
 *
 * One attachment in, the model's own sentence out:
 *
 *   request  `{"attachment_id": 11}`                       (or `{"task_id": 7}` for a task's
 *                                                           single image — the UI always names
 *                                                           the attachment it rendered)
 *   response `{"description": "...", "model": "deepseek-flash", "tokens": 138}`
 *            `tokens` is `null` when the provider did not measure usage — render "unmeasured",
 *            never a fabricated 0.
 *
 * A refusal arrives in the repo envelope (`{ok:false, error:{code, message, how_to_fix}}`) and
 * is raised as the SAME `ApiError` the rest of the task surface renders (`readErrorText` ->
 * `ApiError.display`), so `E_CONFIG` / `E_UPSTREAM` are shown exactly as the server named them.
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
  };
}

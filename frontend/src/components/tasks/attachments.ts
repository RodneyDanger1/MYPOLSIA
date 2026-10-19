import { API_KEY, API_URL, ApiError } from "@/lib/api";

import type { TaskAttachment } from "./types";

/**
 * V25/L1 — the vision-input client half: task image attachments.
 *
 * The shared `api` helper always sends `Content-Type: application/json`, which is wrong for a
 * multipart upload (the browser must set the boundary).  This module is the task-local client
 * for the four attachment routes, and it deliberately reuses `ApiError` so every refusal renders
 * through the SAME `readErrorText` / `ApiError.display` path as the rest of the task surface.
 *
 * Caps are duplicated here as CLIENT-side guards only — the server is authoritative and refuses
 * with `E_VALIDATION` by name; the client guard exists so an obviously unsupported file is not
 * uploaded at all ("no upload on unsupported type"), never to pretend the server would accept it.
 */

export const ATTACHMENT_MIME_TYPES = ["image/png", "image/jpeg", "image/webp"] as const;
export const ATTACHMENT_MAX_BYTES = 5 * 1024 * 1024;
export const ATTACHMENT_MAX_PER_TASK = 4;

const ACCEPT = ATTACHMENT_MIME_TYPES.join(",");

export const ATTACHMENT_ACCEPT = ACCEPT;

/** Why this file cannot be attached — or null when the client sees no problem. */
export function attachmentProblem(file: Pick<File, "type" | "size" | "name">): string | null {
  const type = (file.type || "").toLowerCase();
  if (!(ATTACHMENT_MIME_TYPES as readonly string[]).includes(type)) {
    return `"${file.name}" is not a PNG, JPEG or WebP image — nothing was uploaded.`;
  }
  if (file.size > ATTACHMENT_MAX_BYTES) {
    return `"${file.name}" is ${formatBytes(file.size)}; the limit is 5 MB per image — nothing was uploaded.`;
  }
  if (file.size === 0) {
    return `"${file.name}" is empty — nothing was uploaded.`;
  }
  return null;
}

export function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes < 0) return "unknown size";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

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

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const res = await fetch(`${API_URL}/api/v1${path}`, {
    ...init,
    headers: {
      ...(API_KEY ? { "X-API-Key": API_KEY } : {}),
      ...(init.headers ?? {}),
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
    const requestId = res.headers.get("X-Request-ID") ?? undefined;
    if (refusal) {
      throw new ApiError(res.status, refusal.message, refusal.code, refusal.how_to_fix ?? "", parsed, requestId);
    }
    throw new ApiError(res.status, text || res.statusText, `E_HTTP_${res.status}`, "", undefined, requestId);
  }
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

/** `GET /tasks/{id}/attachments` — metadata, upload order. */
export function listAttachments(taskId: number): Promise<TaskAttachment[]> {
  return request<TaskAttachment[]>(`/tasks/${taskId}/attachments`, { method: "GET" });
}

/**
 * `POST /tasks/{id}/attachments` (multipart).  No `Content-Type` is set: the browser must add
 * the multipart boundary.  A refusal is the server's named `ApiError` (E_VALIDATION / E_CONFIG).
 */
export function uploadAttachment(taskId: number, file: File): Promise<TaskAttachment> {
  const form = new FormData();
  form.append("file", file, file.name);
  return request<TaskAttachment>(`/tasks/${taskId}/attachments`, { method: "POST", body: form });
}

/** `DELETE /attachments/{id}` — 204. */
export function deleteAttachment(attachmentId: number): Promise<void> {
  return request<void>(`/attachments/${attachmentId}`, { method: "DELETE" });
}

/** The image bytes, for a thumbnail (`<img>` cannot send the owner key, so this is a blob). */
export async function fetchAttachmentBlob(attachmentId: number): Promise<Blob> {
  const res = await fetch(`${API_URL}/api/v1/attachments/${attachmentId}`, {
    method: "GET",
    headers: API_KEY ? { "X-API-Key": API_KEY } : {},
  });
  if (!res.ok) {
    const text = await res.text().catch(() => res.statusText);
    throw new ApiError(res.status, text || res.statusText, `E_HTTP_${res.status}`, "");
  }
  return res.blob();
}

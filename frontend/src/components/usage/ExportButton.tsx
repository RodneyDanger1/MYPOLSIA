"use client";

import { useCallback, useState } from "react";

import { Button } from "@/components/ui";
import { API_KEY, ApiError } from "@/lib/api";

/**
 * The ledger's CSV export — GET /api/v1/runs/export?format=csv (backend `runs.py:261-302`).
 *
 * WHY raw `fetch` and not `api.get`: the api client (lib/api.ts) is JSON-only by construction —
 * `apiFetch` ends in `res.json()`, so a CSV body through it would reject after the response
 * arrived — and there is no exported blob mode to extend. This component therefore performs the
 * one read the client cannot, while still taking auth from the client's SINGLE source of truth:
 * `API_KEY` is imported from `@/lib/api` (never a literal, never a second env read), it is sent
 * only when non-empty (the same convention `apiFetch` documents), against the same base-URL
 * convention (`NEXT_PUBLIC_API_URL ?? "http://localhost"`, path prefixed `/api/v1`). The refusal
 * parser is mirrored from lib/api.ts's two documented wire shapes because that module does not
 * export it — a `responseType: "blob"` mode on the client would let this drop the mirror; that is
 * a REQUEST for lib/api.ts's owner, not a change this seat may make.
 *
 * Success is a REAL browser download: blob -> object URL -> anchor[download=runs.csv] click ->
 * revoke. The server's `X-Polsia-Row-Count` header (runs.py:285-289, verified == the row set
 * before the first byte) is surfaced when the CORS layer exposes it to JS; when it is not, the
 * line says exactly what was exported and nothing more. A failure surfaces `ApiError.display`
 * beside the button: never a silent no-op, never a dead button.
 */

const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost";

/** The refusal carried by a response body, in the shapes lib/api.ts parses (envelope + detail). */
function refusalOf(body: unknown): { code: string; message: string; howToFix: string } | null {
  if (!body || typeof body !== "object") return null;
  const b = body as { ok?: unknown; error?: unknown; detail?: unknown };
  const candidate =
    b.ok === false && b.error !== null && typeof b.error === "object"
      ? (b.error as Record<string, unknown>)
      : b.detail !== null && typeof b.detail === "object"
        ? (b.detail as Record<string, unknown>)
        : null;
  if (!candidate || typeof candidate.code !== "string") return null;
  return {
    code: candidate.code,
    message: typeof candidate.message === "string" ? candidate.message : candidate.code,
    howToFix: typeof candidate.how_to_fix === "string" ? candidate.how_to_fix : "",
  };
}

function errorText(e: unknown): string {
  if (e instanceof ApiError) return e.display;
  return e instanceof Error ? e.message : String(e);
}

export function ExportButton() {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState<string | null>(null);

  const onExport = useCallback(async () => {
    setBusy(true);
    setError(null);
    setDone(null);
    try {
      const res = await fetch(`${API_URL}/api/v1/runs/export?format=csv`, {
        headers: { ...(API_KEY ? { "X-API-Key": API_KEY } : {}) },
      });
      if (!res.ok) {
        // Read the refusal BEFORE building the error, so a coded body (E_DB etc.) keeps its
        // code + fix hint instead of degrading to a bare HTTP status.
        const text = await res.text().catch(() => res.statusText);
        let parsed: unknown;
        try {
          parsed = JSON.parse(text);
        } catch {
          parsed = undefined;
        }
        const refusal = refusalOf(parsed);
        throw refusal
          ? new ApiError(res.status, refusal.message, refusal.code, refusal.howToFix, parsed)
          : new ApiError(res.status, text || res.statusText, `E_HTTP_${res.status}`);
      }
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = "runs.csv";
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
      URL.revokeObjectURL(url);
      const declared = res.headers.get("X-Polsia-Row-Count");
      setDone(declared === null ? "Exported runs.csv." : `Exported ${declared} rows to runs.csv.`);
    } catch (e: unknown) {
      setError(errorText(e));
    } finally {
      setBusy(false);
    }
  }, []);

  return (
    <span className="flex items-center gap-2">
      <Button size="sm" data-testid="usage-export" onClick={onExport} disabled={busy}>
        {busy ? "Exporting…" : "Export CSV"}
      </Button>
      {error !== null ? (
        <span role="alert" className="text-sm text-alert" data-testid="usage-export-error">
          Export failed: {error}
        </span>
      ) : null}
      {done !== null ? (
        <span className="ui-label text-ink-muted" data-testid="usage-export-done">
          {done}
        </span>
      ) : null}
    </span>
  );
}

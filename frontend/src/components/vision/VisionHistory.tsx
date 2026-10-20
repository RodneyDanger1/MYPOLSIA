"use client";

import { useEffect, useState } from "react";

import { readErrorText } from "@/components/dashboard/states";
import { cn } from "@/components/ui";

import { listVisionHistory } from "./describe";
import type { VisionHistoryRow } from "./describe";

/**
 * V27 / L4 — the **previous descriptions** block for one task's images.
 *
 * It answers one question honestly: what has the model already said about this task's images,
 * newest first (`GET /api/v1/vision/descriptions?task_id=`)?  Three states, none of them a
 * friendly fiction:
 *
 *  1. rows      — each stored sentence with its model, measured tokens (or "tokens unmeasured"),
 *                 the image's sha256 PREFIX and the stored date;
 *  2. empty     — "No previous descriptions for this task."  An honest empty, not a spinner
 *                 that never resolves;
 *  3. refusal   — the server's named code + message + fix hint (`readErrorText` ->
 *                 `ApiError.display`).  A read that failed is never rendered as "no history".
 *
 * It is a native `<details>` block in the house style (no new visual system): collapsed by
 * default so an attachment row stays compact, and additive — the Describe control and the
 * upload/remove controls around it are unchanged.
 *
 * `version` is the parent's "something was just described" counter: bumping it refetches the
 * history so the block cannot show a stale list beside a fresh answer.
 */
export function VisionHistory({
  taskId,
  version = 0,
  className,
}: {
  taskId: number;
  /** Bumped by the parent after a describe; a NEW number is a NEW read. */
  version?: number;
  className?: string;
}) {
  const [rows, setRows] = useState<VisionHistoryRow[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setRows(null);
    setError(null);
    listVisionHistory(taskId)
      .then((next) => {
        if (!cancelled) setRows(next);
      })
      .catch((e) => {
        if (!cancelled) {
          setRows(null);
          setError(readErrorText(e));
        }
      });
    return () => {
      cancelled = true;
    };
  }, [taskId, version]);

  const count = rows?.length ?? 0;

  return (
    <details
      data-testid={`vision-history-${taskId}`}
      className={cn("mt-2 border border-line bg-surface-sunk p-2", className)}
    >
      <summary className="cursor-pointer text-xs text-ink-muted">
        Previous descriptions{rows && count > 0 ? ` (${count})` : ""}
      </summary>

      {error ? (
        <p
          role="alert"
          data-testid={`vision-history-error-${taskId}`}
          className="mt-1 text-xs text-alert"
        >
          {error}
        </p>
      ) : rows === null ? (
        <p className="mt-1 text-xs text-ink-muted" aria-busy="true">
          Loading previous descriptions…
        </p>
      ) : rows.length === 0 ? (
        <p className="mt-1 text-xs text-ink-muted">No previous descriptions for this task.</p>
      ) : (
        <ul className="mt-1 space-y-1" aria-label="Previous descriptions">
          {rows.map((row) => (
            <li key={row.id} className="border-t border-line pt-1 first:border-t-0 first:pt-0">
              <p className="whitespace-pre-wrap text-sm text-ink">{row.description}</p>
              <p className="mt-0.5 text-xs text-ink-muted">
                {row.model ?? "model unknown"} ·{" "}
                {row.tokens === null ? "tokens unmeasured" : `${row.tokens} tokens`}
                {row.sha256_prefix ? ` · sha ${row.sha256_prefix}` : ""}
                {" · "}
                {row.created_at ? row.created_at.slice(0, 10) : "date unrecorded"}
              </p>
            </li>
          ))}
        </ul>
      )}
    </details>
  );
}

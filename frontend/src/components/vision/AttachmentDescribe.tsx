"use client";

import { useState } from "react";

import { readErrorText } from "@/components/dashboard/states";
import { Button } from "@/components/ui";

import { describeAttachment } from "./describe";
import type { VisionDescription } from "./describe";
import { VisionHistory } from "./VisionHistory";

/**
 * V26 / L4 — the **Describe** action for one attachment.
 * V27 / L4 — plus the task's **previous descriptions** block (memory), when `taskId` is given.
 *
 * Pressing it asks the backend to run the vision model over THIS image and renders the answer
 * inline, in a block labelled "Model description — not saved to the task".  Four honesty rules
 * drive the states below:
 *
 *  1. The description is MODEL OUTPUT for the operator to read.  It is component-local state;
 *     it never becomes the task's result and the task is not refetched or patched here.  What
 *     IS stored is the backend's own `vision_descriptions` row — that is why the history block
 *     below can show it again, and why the label still says it is not the task's result.
 *  2. A refusal is rendered exactly as the server named it (`readErrorText` -> `ApiError.display`,
 *     so `E_CONFIG` / `E_UPSTREAM` and their fix hints stay visible).  There is no fallback
 *     sentence that could pass for a description.
 *  3. `busy` always clears in `finally`: the button never spins forever, whether the call
 *     succeeds, refuses, or the network dies.
 *  4. `cached: true` is stated in the block ("Served from memory — no new model call.").  The
 *     operator can tell a fresh measurement from a remembered one.
 *
 * `tokens: null` means the provider did not report usage — it is rendered "tokens unmeasured",
 * never as a fabricated 0.
 */
export function AttachmentDescribe({
  attachmentId,
  filename,
  taskId,
  className,
}: {
  attachmentId: number;
  filename: string;
  /**
   * The task whose description history is shown below the control.  Optional on purpose: a
   * surface that does not pass it renders exactly what V26 shipped (button + answer + refusal),
   * so the component stays additive for callers that are not this lane's to edit.
   */
  taskId?: number;
  className?: string;
}) {
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<VisionDescription | null>(null);
  const [error, setError] = useState<string | null>(null);
  /** Bumped after every successful describe so the history block refetches (never stale). */
  const [historyVersion, setHistoryVersion] = useState(0);

  const run = async () => {
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      setResult(await describeAttachment(attachmentId));
      setHistoryVersion((version) => version + 1);
    } catch (e) {
      // No description on failure: a stale or invented sentence next to an error would read as
      // the model's answer for THIS press.
      setResult(null);
      setError(readErrorText(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className={className}>
      <Button
        size="sm"
        aria-label={`Describe ${filename}`}
        disabled={busy}
        aria-busy={busy || undefined}
        onClick={() => void run()}
      >
        {busy ? "Describing…" : "Describe"}
      </Button>

      {result ? (
        <div
          role="status"
          data-testid={`vision-description-${attachmentId}`}
          className="mt-2 border border-line bg-surface-sunk p-2"
        >
          <p className="text-xs text-ink-muted">Model description — not saved to the task</p>
          <p className="mt-1 whitespace-pre-wrap text-sm text-ink">{result.description}</p>
          <p className="mt-1 text-xs text-ink-muted">
            Model: {result.model} ·{" "}
            {result.tokens === null ? "tokens unmeasured" : `${result.tokens} tokens`}
          </p>
          {result.cached ? (
            <p className="mt-1 text-xs text-ink-muted">Served from memory — no new model call.</p>
          ) : null}
        </div>
      ) : null}

      {error ? (
        <p
          role="alert"
          data-testid={`vision-error-${attachmentId}`}
          className="mt-1 text-xs text-alert"
        >
          {error}
        </p>
      ) : null}

      {taskId !== undefined ? (
        <VisionHistory taskId={taskId} version={historyVersion} />
      ) : null}
    </div>
  );
}

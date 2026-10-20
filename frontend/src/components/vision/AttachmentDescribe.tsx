"use client";

import { useState } from "react";

import { readErrorText } from "@/components/dashboard/states";
import { Button } from "@/components/ui";

import { describeAttachment } from "./describe";
import type { VisionDescription } from "./describe";

/**
 * V26 / L4 — the **Describe** action for one attachment.
 *
 * Pressing it asks the backend to run the vision model over THIS image and renders the answer
 * inline, in a block labelled "Model description — not saved to the task".  Three honesty
 * rules drive the states below:
 *
 *  1. The description is MODEL OUTPUT for the operator to read.  It is component-local state;
 *     it never becomes the task's result and the task is not refetched or patched here.
 *  2. A refusal is rendered exactly as the server named it (`readErrorText` -> `ApiError.display`,
 *     so `E_CONFIG` / `E_UPSTREAM` and their fix hints stay visible).  There is no fallback
 *     sentence that could pass for a description.
 *  3. `busy` always clears in `finally`: the button never spins forever, whether the call
 *     succeeds, refuses, or the network dies.
 *
 * `tokens: null` means the provider did not report usage — it is rendered "tokens unmeasured",
 * never as a fabricated 0.
 */
export function AttachmentDescribe({
  attachmentId,
  filename,
  className,
}: {
  attachmentId: number;
  filename: string;
  className?: string;
}) {
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<VisionDescription | null>(null);
  const [error, setError] = useState<string | null>(null);

  const run = async () => {
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      setResult(await describeAttachment(attachmentId));
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
    </div>
  );
}

"use client";

import { Button } from "@/components/ui";
import type { ChatConfirmGate } from "@/components/chat/useChat";

/**
 * The confirm gate under a `create_task` reply. Rendering it confirms NOTHING: the single
 * primary (glossy `CONFIRM`) fires `POST /api/v1/chat/confirm` with the reply's single-use
 * token, and every outcome is rendered from what actually happened —
 *
 *  - 200 → the new task id (or a named statement when the server sent no id);
 *  - a 409 / transport failure → the failure's own display string (code + message + fix hint),
 *    with CONFIRM enabled again (an unused token is retryable until it expires);
 *  - `confirming` → the button is disabled and says so.
 */
export function ConfirmCard({
  gate,
  onConfirm,
}: {
  gate: ChatConfirmGate;
  onConfirm: () => void;
}) {
  const { status, taskId, error } = gate;

  return (
    <div
      data-testid="chat-confirm-card"
      className="mt-1 rounded-sm border border-[#E3E3E3] bg-[#FAFAFA] px-3 py-2.5"
    >
      <p className="font-mono text-[10px] uppercase tracking-wider text-[#6e6e6e]">File this task?</p>

      {status === "confirmed" ? (
        <p role="status" className="mt-1 font-serif text-sm leading-snug text-[#111]">
          {typeof taskId === "number"
            ? `Task #${taskId} created.`
            : "Confirmed, but the confirm response carried no task id."}
        </p>
      ) : (
        <>
          <p className="mt-1 font-serif text-xs leading-snug text-[#6e6e6e]">
            CONFIRM files it through the same path the agents page uses. Nothing is created until
            you do.
          </p>
          <div className="mt-2 flex items-center gap-2">
            <Button
              variant="glossy"
              size="sm"
              onClick={onConfirm}
              disabled={status === "confirming"}
            >
              {status === "confirming" ? "CONFIRMING…" : "CONFIRM"}
            </Button>
          </div>
          {status === "failed" && error ? (
            <p
              role="alert"
              className="mt-2 rounded-sm border border-red-200 bg-red-50 px-2 py-1.5 font-mono text-[10px] leading-relaxed text-red-800"
            >
              {error}
            </p>
          ) : null}
        </>
      )}
    </div>
  );
}

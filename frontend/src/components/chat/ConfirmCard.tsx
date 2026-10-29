"use client";

import { Button } from "@/components/ui";
import { absoluteStamp } from "@/components/dashboard/format";
import type { ChatConfirmGate } from "@/components/chat/useChat";

/** Money for a disclosure sentence: two decimals, never a bare float. */
function usd(value: number): string {
  return `$${value.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

/**
 * The confirm gate under a `create_task` reply. Rendering it confirms NOTHING: the single
 * primary (glossy `CONFIRM`) fires `POST /api/v1/chat/confirm` with the reply's single-use
 * token, and every outcome is rendered from what actually happened —
 *
 *  - 200 → the new task id (or a named statement when the server sent no id);
 *  - a 409 / transport failure → the failure's own display string (code + message + fix hint),
 *    with CONFIRM enabled again (an unused token is retryable until it expires);
 *  - `confirming` → the button is disabled and says so.
 *
 * F-29 — THE PAID-RUN DISCLOSURE, BEFORE THE CLICK. The confirm is not just a filing step:
 * `chat_service.confirm_task` creates the task, commits it, and calls `run_agent_task.delay`, so a
 * worker runs the agent against the model provider and real money is spent. The card used to say
 * only "files it through the same path the agents page uses". It now states:
 *
 *   * what the click starts (a real agent run that bills the provider);
 *   * the caps the run is bounded by, as the SERVER declared them on this reply
 *     (`intent.run_cost_cap_usd` / `intent.day_cost_cap_usd`, from
 *     `accounting.budget_policy_from_settings` — the same object the dispatch guard enforces);
 *   * that no estimate exists: the cost is measured AFTER the run (there is no planner estimate on
 *     a chat-filed task, so inventing one here would be a fabricated number);
 *   * when an older server did not send the fields, that they were not declared — never a
 *     made-up cap.
 */
export function ConfirmCard({
  gate,
  onConfirm,
}: {
  gate: ChatConfirmGate;
  onConfirm: () => void;
}) {
  const { status, taskId, error, disclosure } = gate;
  const capsDeclared = disclosure.runCostCapUsd !== null || disclosure.dayCostCapUsd !== null;

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

          {/* F-40 — a gate rebuilt from the STORED proposal says so. After a reload the card used
              to vanish while the message still said "Confirm and I'll queue it"; the server now
              restores the same single-use binding for its ORIGINAL window, and this line states
              that provenance plus the expiry the click is bounded by. */}
          {gate.restored ? (
            <p
              data-testid="chat-confirm-restored"
              className="mt-1 font-mono text-[10px] leading-relaxed text-[#6e6e6e]"
            >
              Restored from the stored proposal — this is the same pending request, and its
              single-use token expires at{" "}
              {gate.expiresAt ? absoluteStamp(gate.expiresAt) : "a time this server did not record"}.
              Confirming here files it exactly as the original card would have.
            </p>
          ) : null}

          {/* F-29 — the disclosure sits between the sentence and the button, so it is read BEFORE
              the click; the costs come from the reply, not from this component. */}
          <div
            data-testid="chat-paid-run-disclosure"
            className="mt-2 rounded-sm border border-amber-200 bg-amber-50 px-2 py-1.5 font-serif text-xs leading-snug text-[#111]"
          >
            <p>
              {disclosure.paidRun === true
                ? "Confirming also dispatches a PAID agent run"
                : "Confirming also dispatches a real agent run"}{" "}
              — a worker calls the model provider and real money is spent.
            </p>
            {capsDeclared ? (
              <p className="mt-1">
                The server caps this run at{" "}
                <span data-testid="chat-run-cost-cap">
                  {disclosure.runCostCapUsd === null
                    ? "no declared per-run cap"
                    : `${usd(disclosure.runCostCapUsd)} per run`}
                </span>
                {disclosure.dayCostCapUsd === null ? null : (
                  <>
                    {" "}
                    and{" "}
                    <span data-testid="chat-day-cost-cap">
                      {usd(disclosure.dayCostCapUsd)} per day
                    </span>
                  </>
                )}
                . Those are the caps the worker enforces before dispatch — the actual cost is
                measured after the run; no estimate exists for a task filed from chat.
              </p>
            ) : (
              <p className="mt-1" data-testid="chat-cost-cap-undeclared">
                This server did not declare the run&rsquo;s cost caps on this reply; the run still
                spends money, and its cost is measured after it runs — no estimate exists for a
                task filed from chat.
              </p>
            )}
          </div>

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

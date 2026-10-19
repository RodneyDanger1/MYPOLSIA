"use client";

import { useEffect, useRef, useState } from "react";

import { errorText } from "@/components/finance/useFinanceRead";
import { ListSkeleton, ReadError } from "@/components/finance/states";
import { fingerprintsFrom } from "@/components/tasks/approval";
import { Button, Card } from "@/components/ui";
import { api } from "@/lib/api";

/**
 * Pending approvals — the parked tasks and the two REAL writes that clear them.
 *
 * The read is the existing task list filtered to the parked status:
 * `GET /tasks?status=awaiting_approval&limit=50` (tasks.py's `status` alias). Each fingerprint is
 * parsed from the task's own `result_summary` by the shipped strict reader
 * (`components/tasks/approval.ts`) — a row with no readable fingerprint says so, and still offers
 * Deny (which needs no token).
 *
 * APPROVE: `POST /approvals {action_fingerprint, task_id}` — the shipped mint + attach + re-queue
 * loop. The minted token is never rendered.
 *
 * DENY: `PATCH /tasks/{id}/status {status:"rejected", error_message}` — the same typed write the
 * task sheet uses, against the `rejected` status the backend's vocabulary carries. Deny is a real
 * state change, so it is two-step (arm, then confirm); a single accidental click cannot refuse work.
 */
export const APPROVALS_LIMIT = 50;
const APPROVALS_PATH = `/tasks?status=awaiting_approval&limit=${APPROVALS_LIMIT}`;

type PendingTask = {
  id: number;
  title: string;
  agent_type: string;
  status: string | null;
  result_summary: string | null;
};

function isPendingList(body: unknown): body is PendingTask[] {
  return (
    Array.isArray(body) &&
    body.every(
      (row) =>
        row !== null &&
        typeof row === "object" &&
        typeof (row as { id?: unknown }).id === "number" &&
        typeof (row as { title?: unknown }).title === "string",
    )
  );
}

export function OpsApprovals({ tick, onChanged }: { tick: number; onChanged: () => void }) {
  const [tasks, setTasks] = useState<PendingTask[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState<string | null>(null);
  const [armedDeny, setArmedDeny] = useState<number | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const alive = useRef(true);

  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);

  useEffect(() => {
    let cancelled = false;
    api
      .get<unknown>(APPROVALS_PATH)
      .then((body) => {
        if (cancelled) return;
        if (!isPendingList(body)) {
          setError("the server returned no pending-approval list");
          setLoading(false);
          return;
        }
        setTasks(body);
        setError(null);
        setLoading(false);
      })
      .catch((e: unknown) => {
        if (cancelled) return;
        setError(errorText(e));
        setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [tick]);

  const approve = async (task: PendingTask, fingerprint: string) => {
    setBusy(`approve:${task.id}:${fingerprint}`);
    setNotice(null);
    setActionError(null);
    try {
      await api.post("/approvals", { action_fingerprint: fingerprint, task_id: task.id });
      if (!alive.current) return;
      setTasks((previous) => previous?.filter((row) => row.id !== task.id) ?? previous);
      setNotice(
        `Approval minted and attached to task #${task.id}; it was re-queued. The token is never displayed.`,
      );
      onChanged();
    } catch (e) {
      if (alive.current) setActionError(errorText(e));
    } finally {
      if (alive.current) setBusy(null);
    }
  };

  const deny = async (task: PendingTask) => {
    if (armedDeny !== task.id) {
      setArmedDeny(task.id);
      setNotice(null);
      setActionError(null);
      return;
    }
    setBusy(`deny:${task.id}`);
    setNotice(null);
    setActionError(null);
    try {
      await api.patch(`/tasks/${task.id}/status`, {
        status: "rejected",
        error_message: "Denied by the operator in Ops — no approval token was minted.",
      });
      if (!alive.current) return;
      setTasks((previous) => previous?.filter((row) => row.id !== task.id) ?? previous);
      setArmedDeny(null);
      setNotice(`Task #${task.id} was denied and marked rejected.`);
      onChanged();
    } catch (e) {
      if (alive.current) setActionError(errorText(e));
    } finally {
      if (alive.current) setBusy(null);
    }
  };

  return (
    <Card
      title="Pending approvals"
      kicker="PARKED RUNS · OWNER DECISION"
      action={
        tasks !== null ? (
          <span className="ui-label text-ink-muted" data-testid="ops-approvals-count">
            {tasks.length} waiting
          </span>
        ) : undefined
      }
    >
      {loading ? <ListSkeleton testId="ops-approvals-loading" /> : null}

      {!loading && error !== null ? (
        <ReadError
          label="pending approvals"
          message={error}
          onRetry={onChanged}
          retryTestId="ops-approvals-retry"
        />
      ) : null}

      {notice !== null ? (
        <p
          role="status"
          data-testid="ops-approvals-notice"
          className="mb-2 border border-line-strong bg-surface-sunk px-3 py-2 text-sm text-ink"
        >
          {notice}
        </p>
      ) : null}

      {actionError !== null ? (
        <p role="alert" data-testid="ops-approvals-action-error" className="mb-2 text-sm text-alert">
          {actionError}
        </p>
      ) : null}

      {!loading && error === null && tasks !== null && tasks.length === 0 ? (
        <p className="text-sm text-ink-muted">
          No tasks are waiting on approval. A run that hits a gated action parks here with the
          fingerprint it needs.
        </p>
      ) : null}

      {!loading && error === null && tasks !== null && tasks.length > 0 ? (
        <ul className="space-y-2" data-testid="ops-approvals-list">
          {tasks.map((task) => {
            const fingerprints = fingerprintsFrom(task.result_summary);
            const denyBusy = busy === `deny:${task.id}`;
            return (
              <li key={task.id} data-testid={`approval-task-${task.id}`} className="border border-line p-2">
                <p className="text-sm font-bold text-ink">{task.title}</p>
                <p className="ui-label text-ink-muted">
                  #{task.id} · {task.agent_type}
                </p>

                {fingerprints.length === 0 ? (
                  <p className="mt-2 text-xs text-ink-muted">
                    This row carries no readable fingerprint, so it cannot be approved from here —
                    denying it is still available.
                  </p>
                ) : (
                  <ul className="mt-2 space-y-1">
                    {fingerprints.map((fingerprint) => {
                      const approveBusy = busy === `approve:${task.id}:${fingerprint}`;
                      return (
                        <li key={fingerprint} className="flex flex-wrap items-center gap-2">
                          <code
                            className="min-w-0 truncate font-mono text-xs text-ink"
                            title={fingerprint}
                          >
                            {fingerprint}
                          </code>
                          <Button
                            size="sm"
                            className="ml-auto"
                            data-testid={`approve-${task.id}-${fingerprint}`}
                            aria-busy={approveBusy || undefined}
                            disabled={busy !== null}
                            onClick={() => void approve(task, fingerprint)}
                          >
                            {approveBusy ? "Approving…" : "Approve"}
                          </Button>
                        </li>
                      );
                    })}
                  </ul>
                )}

                <div className="mt-2 flex items-center gap-2">
                  <Button
                    size="sm"
                    data-testid={`deny-${task.id}`}
                    aria-busy={denyBusy || undefined}
                    disabled={busy !== null}
                    onClick={() => void deny(task)}
                  >
                    {denyBusy ? "Denying…" : armedDeny === task.id ? "Confirm deny" : "Deny"}
                  </Button>
                  {armedDeny === task.id ? (
                    <span className="ui-label text-ink-muted">
                      Deny marks this task rejected — it will not run.
                    </span>
                  ) : null}
                </div>
              </li>
            );
          })}
        </ul>
      ) : null}
    </Card>
  );
}

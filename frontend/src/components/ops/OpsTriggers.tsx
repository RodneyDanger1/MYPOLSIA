"use client";

import { useEffect, useRef, useState } from "react";

import { relativeTime } from "@/components/dashboard/format";
import { errorText } from "@/components/finance/useFinanceRead";
import { ListSkeleton, ReadError } from "@/components/finance/states";
import { Button, Card, DataTable, type Column } from "@/components/ui";
import { api } from "@/lib/api";

/**
 * Manual agent triggers — the existing `POST /agents/{agent_type}/trigger` route, wired.
 *
 * What the verb really does (agents.py): it CREATES a new task row (`source="manual"`) and hands it
 * to a worker; it never re-runs an existing task. The copy says so, and the success notice quotes
 * the server's own message (which names the new task id). A refusal is rendered by
 * `ApiError.display` — code, message and fix hint — never rewritten into a friendlier sentence.
 *
 * While one dispatch is in flight every trigger is disabled: the verb creates rows, so a double
 * click would create two tasks. A failed dispatch re-enables them and names why.
 */
type AgentRow = {
  agent_type: string;
  last_run_at: string | null;
  last_run_status: string | null;
  tasks_today: number;
  tasks_total: number;
};

function isAgentList(body: unknown): body is AgentRow[] {
  return (
    Array.isArray(body) &&
    body.every(
      (row) =>
        row !== null &&
        typeof row === "object" &&
        typeof (row as { agent_type?: unknown }).agent_type === "string",
    )
  );
}

const COLUMNS: Column<AgentRow & { onTrigger: () => void; busy: boolean; anyBusy: boolean }>[] = [
  { key: "agent", header: "AGENT", render: (row) => row.agent_type },
  {
    key: "last",
    header: "LAST RUN",
    render: (row) =>
      row.last_run_at === null ? (
        <span className="text-ink-muted">never</span>
      ) : (
        <span title={row.last_run_status ?? undefined}>{relativeTime(row.last_run_at)}</span>
      ),
  },
  { key: "today", header: "TODAY", render: (row) => String(row.tasks_today) },
  { key: "total", header: "ALL TIME", render: (row) => String(row.tasks_total) },
  {
    key: "action",
    header: "DISPATCH",
    render: (row) => (
      <Button
        size="sm"
        data-testid={`trigger-${row.agent_type}`}
        aria-label={`Trigger ${row.agent_type}`}
        aria-busy={row.busy || undefined}
        disabled={row.anyBusy}
        onClick={row.onTrigger}
      >
        {row.busy ? "Queueing…" : "Trigger"}
      </Button>
    ),
  },
];

export function OpsTriggers({ tick, onRetry }: { tick: number; onRetry?: () => void }) {
  const [agents, setAgents] = useState<AgentRow[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState<string | null>(null);
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
      .get<unknown>("/agents/status")
      .then((body) => {
        if (cancelled) return;
        if (!isAgentList(body)) {
          setError("the server returned no agent status list");
          setLoading(false);
          return;
        }
        setAgents(body);
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

  const trigger = async (agentType: string) => {
    if (busy !== null) return;
    setBusy(agentType);
    setNotice(null);
    setActionError(null);
    try {
      const result = await api.post<{ status: string; task_id: number; message: string }>(
        `/agents/${agentType}/trigger`,
        { task_title: `Manual ${agentType} run (Ops)` },
      );
      if (!alive.current) return;
      setNotice(result.message);
    } catch (e) {
      if (alive.current) setActionError(errorText(e));
    } finally {
      if (alive.current) setBusy(null);
    }
  };

  const rows: (AgentRow & { onTrigger: () => void; busy: boolean; anyBusy: boolean })[] =
    (agents ?? []).map((agent) => ({
      ...agent,
      onTrigger: () => void trigger(agent.agent_type),
      busy: busy === agent.agent_type,
      anyBusy: busy !== null,
    }));

  return (
    <Card title="Agent triggers" kicker="MANUAL DISPATCH · CREATES A NEW TASK">
      <p className="text-sm text-ink-muted">
        Triggering an agent creates a <strong>new</strong> manual task row for it and hands that row
        to a worker — it does not re-run an existing task. The notice below is the server&rsquo;s own
        message, including the new task id.
      </p>

      {loading ? <ListSkeleton testId="ops-triggers-loading" /> : null}

      {!loading && error !== null ? (
        <ReadError
          label="the agent roster"
          message={error}
          onRetry={onRetry}
          retryTestId="ops-triggers-retry"
        />
      ) : null}

      {notice !== null ? (
        <p
          role="status"
          data-testid="ops-trigger-notice"
          className="mt-2 border border-line-strong bg-surface-sunk px-3 py-2 text-sm text-ink"
        >
          {notice}
        </p>
      ) : null}

      {actionError !== null ? (
        <p role="alert" data-testid="ops-trigger-error" className="mt-2 text-sm text-alert">
          {actionError}
        </p>
      ) : null}

      {!loading && error === null && agents !== null ? (
        <div className="mt-2" data-testid="ops-triggers-table">
          <DataTable<(typeof rows)[number]>
            label="Agent roster and manual trigger controls"
            columns={COLUMNS}
            rows={rows}
            rowKey={(row) => row.agent_type}
            empty={
              <p className="text-sm text-ink-muted">
                No agent statuses came back — the registry read served no rows.
              </p>
            }
          />
        </div>
      ) : null}
    </Card>
  );
}

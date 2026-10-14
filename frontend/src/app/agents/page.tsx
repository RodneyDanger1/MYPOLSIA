"use client";

import { useState } from "react";

import { AgentsRoster } from "@/components/agents/AgentsRoster";
import { agentLabel } from "@/components/dashboard/format";
import { errorText } from "@/components/tasks/status";
import type { AgentTriggerResponse } from "@/components/tasks/types";
import { cn } from "@/components/ui";
import { api } from "@/lib/api";

type Banner = { agent: string; text: string; isError: boolean };

/**
 * The agents page — an OURS-DESIGN surface (agents-page.md: the live product was never observed
 * to have one; live's Run Now is per task). Every region below is fed by a real endpoint:
 * `GET /agents/status` (via `useAgentStatus`, the audit's model implementation — the only
 * poller) and `POST /agents/{agent_type}/trigger` for the per-row run.
 *
 * Trigger flow, end to end and honest:
 *   - `Run now` opens a confirm that NAMES the agent and says a new task will be created
 *     (B21-lite); cancelling issues no request.
 *   - One in-flight trigger per agent (a per-agent pending map — Q-20: previously a single
 *     shared slot let two runs race and the loser's result vanish); the banner names the agent
 *     it reports on.
 *   - On 202 the server's `message` is shown verbatim and the roster is REMOUNTED (via `key`),
 *     which performs a fresh `GET /agents/status` — the Q-20 "no refetch after a trigger" fix,
 *     without touching the shared hook or adding a second poller.
 *   - A refusal shows `ApiError.display` (code + how_to_fix) in the `role="alert"` banner; the
 *     success styling is never reused for it (`data-tone` pins the two apart).
 */
export default function AgentsPage() {
  const [pending, setPending] = useState<Record<string, boolean>>({});
  const [confirmAgent, setConfirmAgent] = useState<string | null>(null);
  const [banner, setBanner] = useState<Banner | null>(null);
  const [refreshSeq, setRefreshSeq] = useState(0);

  const trigger = async (agentType: string) => {
    setConfirmAgent(null);
    setPending((current) => ({ ...current, [agentType]: true }));
    try {
      const result = await api.post<AgentTriggerResponse>(`/agents/${agentType}/trigger`, {
        // The title makes the created row self-describing on /tasks (OPEN DECISION #1).
        task_title: `Manual run — ${agentLabel(agentType)}`,
        description: "Triggered from the Agents page.",
      });
      setBanner({ agent: agentLabel(agentType), text: result.message, isError: false });
      setRefreshSeq((n) => n + 1);
    } catch (e) {
      setBanner({ agent: agentLabel(agentType), text: errorText(e), isError: true });
    } finally {
      setPending((current) => {
        const next = { ...current };
        delete next[agentType];
        return next;
      });
    }
  };

  return (
    <div className="space-y-4">
      <div className="border-b border-line pb-2">
        <h1 className="text-3xl font-bold text-ink">Agents</h1>
      </div>

      {banner ? (
        <div
          role={banner.isError ? "alert" : "status"}
          data-tone={banner.isError ? "error" : "queued"}
          className={cn(
            "border p-3",
            banner.isError ? "border-alert bg-surface" : "border-line-strong bg-surface-sunk",
          )}
        >
          <p className={cn("ui-label", banner.isError ? "text-alert" : "text-ink")}>
            {banner.agent} — {banner.isError ? "refused" : "queued"}
          </p>
          <p className="mt-1 text-sm text-ink">{banner.text}</p>
        </div>
      ) : null}

      <AgentsRoster
        key={refreshSeq}
        pending={pending}
        confirmAgent={confirmAgent}
        onRunRequest={(agentType) => setConfirmAgent(agentType)}
        onRunCancel={() => setConfirmAgent(null)}
        onRunConfirm={(agentType) => void trigger(agentType)}
      />
    </div>
  );
}

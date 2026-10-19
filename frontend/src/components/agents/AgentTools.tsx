"use client";

import { useId, useRef, useState } from "react";
import type { KeyboardEvent } from "react";

import { Button } from "@/components/ui";

import { toolsForAgent } from "./toolbox";
import type { AgentToolboxRead } from "./useAgentToolbox";

/**
 * One agent's expandable toolbox on the Agents page (owner wish, V20 L4): the tools THAT agent can
 * use, read from `GET /api/v1/ops/capabilities` through the shared `useAgentToolbox` read and the
 * capabilities page's own `capabilitiesFrom` parser.
 *
 * States, all honest:
 *   - collapsed by default; the button carries `aria-expanded` + `aria-controls`;
 *   - loading — named, never an empty list;
 *   - failed — the dashboard's shared `readErrorText` sentence (`ApiError.display`, code + fix
 *     hint) under `role="alert"` with a Retry that re-reads; the button itself says "unavailable"
 *     so the failure is visible before expanding, and a failure is NEVER rendered as "no tools";
 *   - answered with tools — name + the registry's own description verbatim; a missing description
 *     is MARKED, not embellished;
 *   - answered with an empty tool list for a listed agent — "No tools are granted to this agent";
 *   - answered with no readable row for this agent (a row the parser had to drop, or an agent the
 *     map does not mention) — said as exactly that, which is a different fact from "no tools";
 *   - answered with an empty `agents[]` — said as such, never projected into "every agent has none".
 *
 * Keyboard: Escape collapses from anywhere inside the section and returns focus to the button.
 * No storage, no logging, no network of its own (the one read belongs to the roster).
 */
export function AgentTools({
  agentType,
  toolbox,
}: {
  agentType: string;
  toolbox: AgentToolboxRead;
}) {
  const [open, setOpen] = useState(false);
  const panelId = useId();
  const toggleRef = useRef<HTMLButtonElement>(null);
  const { read, loading, error, reload } = toolbox;
  const view = toolsForAgent(read, agentType);

  const label = loading
    ? "Tools"
    : error !== null
      ? "Tools — unavailable"
      : view.known
        ? `Tools · ${view.tools.length}`
        : "Tools";

  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (!open || event.key !== "Escape") return;
    event.preventDefault();
    event.stopPropagation();
    setOpen(false);
    toggleRef.current?.focus();
  };

  const body = () => {
    if (loading) {
      return (
        <p aria-busy="true" className="ui-label text-ink-muted">
          Loading the tool registry…
        </p>
      );
    }
    if (error !== null) {
      return (
        <div role="alert" data-testid="agents-tools-error">
          <p className="ui-label text-alert">Could not load this agent&rsquo;s toolbox</p>
          <p className="mt-1 text-sm text-ink">{error}</p>
          <button type="button" className="link ui-label mt-2" onClick={reload}>
            Retry
          </button>
        </div>
      );
    }
    if (view.tools.length > 0) {
      return (
        <ul className="space-y-2">
          {view.tools.map((tool) => (
            <li key={tool.name}>
              <p className="ui-label text-ink">{tool.name}</p>
              <p className="mt-1 text-sm text-ink-muted">
                {tool.description !== ""
                  ? tool.description
                  : "The registry states no description for this tool."}
              </p>
            </li>
          ))}
        </ul>
      );
    }
    if (view.known) {
      return <p className="text-sm text-ink-muted">No tools are granted to this agent.</p>;
    }
    if (read !== null && read.map.agents.length === 0) {
      return (
        <p className="text-sm text-ink-muted">
          The capability map lists no agents — no tools can be shown for this agent.
        </p>
      );
    }
    return (
      <p className="text-sm text-ink-muted">
        The capability map has no readable entry for this agent.
      </p>
    );
  };

  return (
    <div
      data-testid={`agent-tools-${agentType}`}
      data-agent-tools={agentType}
      className="mt-3 border-t border-line pt-2"
      onKeyDown={onKeyDown}
    >
      <Button
        ref={toggleRef}
        size="sm"
        aria-expanded={open}
        aria-controls={open ? panelId : undefined}
        onClick={() => setOpen((current) => !current)}
      >
        {label}
      </Button>
      {open ? (
        <div
          id={panelId}
          role="group"
          aria-label={`Tools for ${agentType}`}
          className="mt-2 border border-line bg-surface-sunk p-3"
        >
          {body()}
          {!loading && error === null && read !== null && read.unreadable > 0 ? (
            <p className="ui-label mt-2 text-ink-muted">
              {read.unreadable} unreadable {read.unreadable === 1 ? "row" : "rows"} dropped from the
              map.
            </p>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

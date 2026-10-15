import { Chip, SectionHeader } from "@/components/ui";

import type { CapabilitiesRead, CapabilityAgent, CapabilityTool } from "@/components/capabilities/types";

/**
 * The rendered capabilities map (H-C1). Presentational only — every string on screen came off the
 * read verbatim; the `unreadable` count is the page's one derived number and it only ever COUNTS
 * dropped rows (it never fills them in). Three honest empties, one per scope:
 *   - both arrays empty  -> the whole-page empty state;
 *   - no agents          -> a named line where the agent cards would be (catalog still renders);
 *   - no registry tools  -> a named line inside the catalog section (agent cards still render);
 *   - per agent: no tools / no channels -> named lines inside that agent's card.
 */

function toolDescription(tool: CapabilityTool): string {
  return tool.description.trim() !== "" ? tool.description : "The registry states no description for this tool.";
}

function ToolRow({ tool }: { tool: CapabilityTool }) {
  return (
    <li className="text-sm text-ink">
      <code className="font-mono text-xs">{tool.name}</code>
      <span className="ml-2 text-ink-muted">{toolDescription(tool)}</span>
    </li>
  );
}

function AgentCard({ agent }: { agent: CapabilityAgent }) {
  return (
    <section className="card-surface p-4" data-testid={`capability-agent-${agent.agent_type}`}>
      <h2 className="text-lg font-bold leading-tight text-ink">{agent.agent_type}</h2>

      <div className="mt-3">
        <p className="ui-label text-ink-muted">Action classes</p>
        {agent.action_classes.length > 0 ? (
          <div className="mt-1 flex flex-wrap gap-1">
            {agent.action_classes.map((actionClass) => (
              <Chip key={actionClass} variant="soft">
                {actionClass}
              </Chip>
            ))}
          </div>
        ) : (
          <p className="mt-1 text-sm text-ink-muted">No action classes granted.</p>
        )}
      </div>

      <div className="mt-3">
        <p className="ui-label text-ink-muted">Channels</p>
        {agent.channels.length > 0 ? (
          <div className="mt-1 flex flex-wrap gap-1">
            {agent.channels.map((channel) => (
              <Chip key={channel} variant="dark">
                {channel}
              </Chip>
            ))}
          </div>
        ) : (
          <p className="mt-1 text-sm text-ink-muted">No external channels.</p>
        )}
      </div>

      <div className="mt-3">
        <p className="ui-label text-ink-muted">Registry tools</p>
        {agent.tools.length > 0 ? (
          <ul className="mt-1 space-y-1">
            {agent.tools.map((tool) => (
              <ToolRow key={tool.name} tool={tool} />
            ))}
          </ul>
        ) : (
          <p className="mt-1 text-sm text-ink-muted">No registry tools are granted to this agent.</p>
        )}
      </div>
    </section>
  );
}

export function CapabilityMapView({ read }: { read: CapabilitiesRead }) {
  const { agents, registry_tools } = read.map;
  const agentCount = agents.length;
  const toolCount = registry_tools.length;
  const empty = agentCount === 0 && toolCount === 0;

  return (
    <div className="space-y-4">
      {/* A count of what THIS read measured — never a claimed registry size. */}
      <p className="ui-label text-ink-muted" data-testid="capabilities-count">
        {agentCount} {agentCount === 1 ? "agent" : "agents"} · {toolCount}{" "}
        {toolCount === 1 ? "registry tool" : "registry tools"}
      </p>

      {/* A malformed row is DROPPED and NAMED here — silence would overstate the map. */}
      {read.unreadable > 0 ? (
        <p className="ui-label text-alert" data-testid="capabilities-dropped">
          {read.unreadable} unreadable {read.unreadable === 1 ? "row" : "rows"} dropped — the
          registry served {read.unreadable === 1 ? "a row" : "rows"} this page cannot read.
        </p>
      ) : null}

      {empty ? (
        <div className="card-surface p-4" data-testid="capabilities-empty">
          <p className="text-sm text-ink">
            The registry answered an empty map: no agents and no registry tools.
          </p>
          <p className="ui-label mt-1 text-ink-muted">
            Nothing is being hidden — the policy firewall and the tool registry of this build
            genuinely hold no rows yet.
          </p>
        </div>
      ) : (
        <>
          {agentCount > 0 ? (
            <div className="space-y-3">
              {agents.map((agent) => (
                <AgentCard key={agent.agent_type} agent={agent} />
              ))}
            </div>
          ) : (
            <p className="text-sm text-ink-muted">
              The registry answered no agents — only the tool catalog below is populated.
            </p>
          )}

          <section className="card-surface p-4" data-testid="capabilities-registry">
            <SectionHeader kicker="what any agent can call" title="Registry tools" />
            {toolCount > 0 ? (
              <ul className="mt-2 space-y-1">
                {registry_tools.map((tool) => (
                  <ToolRow key={tool.name} tool={tool} />
                ))}
              </ul>
            ) : (
              <p className="mt-2 text-sm text-ink-muted">The tool registry answered zero tools.</p>
            )}
          </section>
        </>
      )}
    </div>
  );
}

/**
 * The Agents-page view over the ONE capabilities read (`capabilitiesFrom`, H-C1). This module is
 * NOT a second wire parser: it only indexes the already-parsed map, so the page and `/capabilities`
 * can never disagree about what the payload said.
 *
 * Duplicate `agent_type` rows (a hostile or buggy payload) are MERGED: every tool granted by any
 * row for that agent is shown once, first-seen order, deduped by name. Dropping the later row would
 * hide a real grant; rendering it twice would invent a duplicate.
 */
import type { CapabilitiesRead, CapabilityTool } from "@/components/capabilities/types";

export type AgentToolboxView = {
  /** That agent's tools, merged across duplicate rows and deduped by name. */
  tools: CapabilityTool[];
  /** The map carries at least one readable row for this agent (empty tools is a real answer). */
  known: boolean;
};

export function toolsForAgent(read: CapabilitiesRead | null, agentType: string): AgentToolboxView {
  if (read === null) return { tools: [], known: false };
  const tools: CapabilityTool[] = [];
  const seen = new Set<string>();
  let known = false;
  for (const agent of read.map.agents) {
    if (agent.agent_type !== agentType) continue;
    known = true;
    for (const tool of agent.tools) {
      if (seen.has(tool.name)) continue;
      seen.add(tool.name);
      tools.push(tool);
    }
  }
  return { tools, known };
}

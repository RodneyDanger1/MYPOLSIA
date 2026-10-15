/**
 * The capabilities map wire types + the ONE parser for `GET /api/v1/ops/capabilities` (frozen
 * interface H-C1, wave V16).
 *
 * The endpoint serves `data = {agents:[{agent_type, tools:[{name, description}],
 * action_classes:[str], channels:[str]}], registry_tools:[{name, description}]}` — DERIVED
 * server-side from `policy_firewall.AGENT_CAPABILITIES` + `tool_registry` (never hand-copied).
 * This page renders exactly what that read answers; it is a VIEW, so its honesty law is:
 *
 *   - nothing is invented: every name / description / action class / channel rendered here came
 *     off the wire verbatim; a tool row without a description says so instead of being embellished;
 *   - BOTH success shapes render — the plan's envelope `{ok: true, data: {...}}` and the bare map
 *     today's sibling ops routes serve — while a body carrying NEITHER fails LOUDLY here (a named
 *     shape error the page renders as its error state), never as an empty map;
 *   - a row the page cannot read is DROPPED and COUNTED (`unreadable`), so the page can name it,
 *     while every readable row still renders (the malformed-row discipline: skip the bad item with
 *     a named warning, never abort the batch, never fabricate the row).
 */

export type CapabilityTool = {
  name: string;
  /** The registry's own words; `""` means the registry states no description (rendered as such). */
  description: string;
};

export type CapabilityAgent = {
  agent_type: string;
  tools: CapabilityTool[];
  action_classes: string[];
  channels: string[];
};

export type CapabilityMap = {
  agents: CapabilityAgent[];
  registry_tools: CapabilityTool[];
};

export type CapabilitiesRead = {
  map: CapabilityMap;
  /** Rows anywhere in the body that a reader could not make sense of (counted, never rendered). */
  unreadable: number;
};

function isObject(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

/** One tool row, or `null` when it carries no usable name. A non-string description degrades to
 *  `""` = "the registry states none" — it never becomes a fabricated sentence. */
function toolOf(row: unknown): CapabilityTool | null {
  if (!isObject(row)) return null;
  if (typeof row.name !== "string" || row.name.trim() === "") return null;
  return { name: row.name, description: typeof row.description === "string" ? row.description : "" };
}

/** One agent row: its four frozen fields must exist and be arrays — a row missing them is dropped
 *  whole (rendering half an agent would be a fabricated row). Structurally valid rows whose tool /
 *  class / channel ENTRIES are junk drop those entries only, counted. */
function agentOf(row: unknown): { agent: CapabilityAgent; dropped: number } | null {
  if (!isObject(row)) return null;
  if (typeof row.agent_type !== "string" || row.agent_type.trim() === "") return null;
  if (!Array.isArray(row.tools) || !Array.isArray(row.action_classes) || !Array.isArray(row.channels)) {
    return null;
  }
  let dropped = 0;
  const tools: CapabilityTool[] = [];
  for (const entry of row.tools) {
    const tool = toolOf(entry);
    if (tool === null) dropped += 1;
    else tools.push(tool);
  }
  const action_classes = row.action_classes.filter((c): c is string => typeof c === "string");
  const channels = row.channels.filter((c): c is string => typeof c === "string");
  dropped += row.action_classes.length - action_classes.length + (row.channels.length - channels.length);
  return { agent: { agent_type: row.agent_type, tools, action_classes, channels }, dropped };
}

/** The envelope unwrap. The plan's H-C1 envelope is `{ok: true, data: {…}}`; the sibling ops
 *  routes serve the bare map. A `{data: {…}}` body whose `data` LOOKS like the map (carries one of
 *  the two frozen arrays) is unwrapped too — a 200 body with an unrelated `data` key is not
 *  assumed to be the envelope and falls through to the bare-map reader (which names it broken). */
function unwrapCapabilities(body: unknown): unknown {
  if (!isObject(body)) return body;
  if (body.ok === true && isObject(body.data)) return body.data;
  if (isObject(body.data)) {
    const inner = body.data;
    if (Array.isArray(inner.agents) || Array.isArray(inner.registry_tools)) return inner;
  }
  return body;
}

/**
 * Parse the read body into the render model. THROWS (with a named message the page renders as its
 * error state) when the body is not the capability map at all — the empty state is reserved for a
 * read that genuinely answered an empty map, so a broken read can never masquerade as "no data".
 */
export function capabilitiesFrom(body: unknown): CapabilitiesRead {
  const source = unwrapCapabilities(body);
  if (!isObject(source)) {
    throw new Error(
      "the server returned no capability map (the body is not an object) — this is not the H-C1 payload",
    );
  }
  if (!Array.isArray(source.agents) || !Array.isArray(source.registry_tools)) {
    throw new Error(
      "the server returned no capability map (agents / registry_tools arrays are absent — this is not the H-C1 payload)",
    );
  }
  let unreadable = 0;
  const agents: CapabilityAgent[] = [];
  for (const row of source.agents) {
    const parsed = agentOf(row);
    if (parsed === null) unreadable += 1;
    else {
      agents.push(parsed.agent);
      unreadable += parsed.dropped;
    }
  }
  const registry_tools: CapabilityTool[] = [];
  for (const row of source.registry_tools) {
    const tool = toolOf(row);
    if (tool === null) unreadable += 1;
    else registry_tools.push(tool);
  }
  return { map: { agents, registry_tools }, unreadable };
}

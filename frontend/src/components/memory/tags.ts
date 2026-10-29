/**
 * F-42 (wave 8) — the stored row's TAGS, narrowed for display.
 *
 * THE WIRE: `MemoryItem.tags` is `list[str] | None` (`backend/app/schemas/memory.py`) and the plain
 * LIST read (`GET /memory?limit=…`, no `q`) carries it — measured live 2026-10-29: 50/50 rows
 * tagged (`claim:banned-current`, `claim:reviewed:2026-10-29`, `company_id:1`, …).
 *
 * THE DEFECT THIS CLOSES: `/memory` only ever issued the SEMANTIC-SEARCH read (`?q=`), whose hits
 * are built from Chroma metadata and carry no tags at all (`memory_service.search_memory`), so the
 * screen rendered none of a fact the other branch of the same endpoint was serving. The screen now
 * offers an explicit "Recent entries" browse against the LIST branch (the page's read-on-request
 * law is preserved) and renders each row's tags; when a search row carries no tags (today's wire)
 * nothing is claimed about them.
 *
 * The narrowing is deliberately strict: a tag is a NON-BLANK STRING. A non-array value, numbers,
 * nulls and blanks are absence — never a chip reading "undefined" or an empty pill. Order and
 * case are preserved (a tag is an identifier, not prose) and duplicates are collapsed so one row
 * cannot print the same label twice.
 */

/** How many tags a row prints before the rest are named as "+N more" (bounded display). */
export const MEMORY_TAGS_SHOWN = 6;

/** The usable tags on a row, deduped, in wire order. Junk shapes are absence, never chips. */
export function memoryTags(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  const out: string[] = [];
  const seen = new Set<string>();
  for (const entry of value) {
    if (typeof entry !== "string") continue;
    const tag = entry.trim();
    if (tag === "" || seen.has(tag)) continue;
    seen.add(tag);
    out.push(tag);
  }
  return out;
}

/** The tags printed on the row: the first `MEMORY_TAGS_SHOWN`, in wire order. */
export function shownTags(tags: string[]): string[] {
  return tags.slice(0, MEMORY_TAGS_SHOWN);
}

/** How many tags the "+N more" chip stands for (0 = no chip). */
export function hiddenTagCount(tags: string[]): number {
  return Math.max(0, tags.length - MEMORY_TAGS_SHOWN);
}

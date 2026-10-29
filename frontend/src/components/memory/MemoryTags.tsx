"use client";

import { Chip } from "@/components/ui";
import { hiddenTagCount, memoryTags, shownTags } from "@/components/memory/tags";

/**
 * F-42 (wave 8) — a memory row's stored tags, bounded and honest.
 *
 * * nothing to show -> NOTHING renders (no "no tags" claim: an absent list on a search hit means
 *   the branch did not carry them, not that the row has none);
 * * up to `MEMORY_TAGS_SHOWN` chips are printed, the remainder as "+N more" whose `title` lists
 *   them in full, so a long tag set never stretches the row;
 * * each chip carries its full tag in `title` and truncates visually only (`max-w` + `truncate`),
 *   so the TEXT is never altered — a tag is an identifier (`claim:reviewed:2026-10-29`).
 */
export function MemoryTags({ tags }: { tags: unknown }) {
  const all = memoryTags(tags);
  if (all.length === 0) return null;
  const shown = shownTags(all);
  const hidden = hiddenTagCount(all);
  return (
    <div
      data-testid="memory-tags"
      className="mt-1 flex flex-wrap items-center gap-1"
      aria-label={`Tags (${all.length})`}
    >
      <span className="ui-label text-ink-muted">Tags</span>
      {shown.map((tag) => (
        <Chip
          key={tag}
          variant="soft"
          data-testid="memory-tag"
          title={tag}
          className="max-w-[14rem] truncate"
        >
          {tag}
        </Chip>
      ))}
      {hidden > 0 ? (
        <Chip
          variant="soft"
          data-testid="memory-tags-more"
          title={all.slice(shown.length).join(", ")}
        >
          +{hidden} more
        </Chip>
      ) : null}
    </div>
  );
}

"use client";

import { absoluteStamp, relativeTime } from "@/components/dashboard/format";
import { Chip, cn } from "@/components/ui";

import { inboxStatusChipClass, inboxStatusLabel } from "./inboxStatus";
import type { SupportInboxItem } from "./types";

/**
 * One inbox row. Honest rendering of the schema's nullables (`SupportInboxItemResponse`):
 *   * `received_at` null → the age shown is the CREATED age and the row says "arrival not
 *     stated" (never a fabricated receipt time — the same discipline as the tasks card's
 *     `created …` fallback);
 *   * both timestamps null → `relativeTime` says "date unknown" (never "Invalid Date");
 *   * `task_id` null → no task link is drawn, and the row says the item is not attached yet.
 *
 * The task link is a plain `<a>` to `/tasks/{id}` — the [id] route renders the same detail the
 * board's sheet does.
 */
export function InboxItemRow({ item }: { item: SupportInboxItem }) {
  return (
    <li className="border-b border-line px-3 py-3 last:border-b-0" data-testid={`support-item-${item.id}`}>
      <div className="flex flex-wrap items-baseline gap-2">
        <p className="min-w-0 truncate text-sm font-bold text-ink" title={item.from_address}>
          {item.from_address}
        </p>
        <Chip variant="soft" className={cn("shrink-0", inboxStatusChipClass(item.status))}>
          {inboxStatusLabel(item.status)}
        </Chip>
        <span className="ui-label ml-auto shrink-0 text-ink-muted">
          {item.received_at ? (
            <span title={`received ${absoluteStamp(item.received_at)}`}>
              received {relativeTime(item.received_at)}
            </span>
          ) : (
            <span
              title={
                item.created_at
                  ? `created ${absoluteStamp(item.created_at)}`
                  : "no timestamp was recorded for this item"
              }
            >
              created {relativeTime(item.created_at)} — arrival not stated
            </span>
          )}
        </span>
      </div>

      <p className="mt-1 text-sm text-ink">{item.subject}</p>
      <p className="mt-1 line-clamp-2 text-xs text-ink-muted">{item.body}</p>

      <div className="mt-2 flex flex-wrap items-center gap-2">
        <span className="ui-label text-ink-muted" title={item.provider_message_id}>
          provider id: {item.provider_message_id}
        </span>
        {item.task_id !== null ? (
          <a href={`/tasks/${item.task_id}`} className="link ui-label ml-auto">
            Open task #{item.task_id}
          </a>
        ) : (
          <span className="ui-label ml-auto text-ink-muted">
            not attached to a task{item.status === "dispatched" ? " — the mark says dispatched but no task id was recorded" : " yet"}
          </span>
        )}
      </div>
    </li>
  );
}

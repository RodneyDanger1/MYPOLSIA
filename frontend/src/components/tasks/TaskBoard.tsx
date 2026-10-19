"use client";

import { useId, useState } from "react";

import { TaskCard } from "./TaskCard";
import type { TaskRow } from "./types";

/**
 * The board's read size. One call feeds both columns (tasks.md §2): the API returns a bare list
 * with no total (`audit Q-12`), so the maximum page is asked for and a response that fills the
 * page is LABELLED as truncated by `app/tasks/page.tsx` — never silently, and never with an
 * invented count. Declared here (not in the page module) because Next's generated route types
 * reject any non-route export from `page.tsx`.
 */
export const TASKS_BOARD_LIMIT = 500;

/**
 * Board bucketing. TO DO / DONE are the live board's two captured columns (cap-05: `▸ TO DO` /
 * `✓ DONE`, counts right-aligned, hairline under each). The terminal-negative bucket is
 * tasks.md OPEN DECISION #2's recommended `✗ NOT DONE` (visible beats hidden), and a fourth
 * `UNKNOWN STATUS` bucket holds rows whose `status` is null or a word this build does not know —
 * the column is nullable on the wire, and silently dropping such a row would be the Q-12
 * truncation-honesty defect wearing a different hat. The two live columns always render; the
 * extra buckets render only when they carry rows.
 *
 * `OPEN_STATUSES` is the non-terminal set that feeds TO DO: pending, in progress and awaiting
 * approval are all still open work, so none of them may fall into DONE or be dropped. The name
 * describes that contract (it is a status set, not a task marker); the TO DO column label is the
 * surface's, not the set's.
 */
const OPEN_STATUSES = new Set(["pending", "in_progress", "awaiting_approval"]);
const DONE_STATUSES = new Set(["completed"]);
const NOT_DONE_STATUSES = new Set(["failed", "rejected", "cancelled"]);

type Group = {
  key: string;
  label: string;
  glyph: string;
  rows: TaskRow[];
  alwaysVisible: boolean;
};

export function groupTasks(rows: TaskRow[]): Group[] {
  const buckets: Record<string, TaskRow[]> = { todo: [], done: [], notdone: [], unknown: [] };
  for (const row of rows) {
    const status = row.status;
    if (status && OPEN_STATUSES.has(status)) buckets.todo.push(row);
    else if (status && DONE_STATUSES.has(status)) buckets.done.push(row);
    else if (status && NOT_DONE_STATUSES.has(status)) buckets.notdone.push(row);
    else buckets.unknown.push(row);
  }
  return [
    { key: "todo", label: "TO DO", glyph: "▸", rows: buckets.todo, alwaysVisible: true },
    { key: "done", label: "DONE", glyph: "✓", rows: buckets.done, alwaysVisible: true },
    { key: "notdone", label: "NOT DONE", glyph: "✗", rows: buckets.notdone, alwaysVisible: false },
    { key: "unknown", label: "UNKNOWN STATUS", glyph: "?", rows: buckets.unknown, alwaysVisible: false },
  ];
}

/**
 * The two/three-column board (tasks.md §1 R3). Desktop: TO DO and DONE side by side; any extra
 * bucket wraps below in the same grid. Narrow viewports stack to a single column — the live
 * mobile capture does not exist (UNVERIFIED in the spec), so the stacking behaviour is ours.
 * Each column scrolls independently (live shows the TO DO column's own scrollbar).
 */
export function TaskBoard({ rows, onOpen }: { rows: TaskRow[]; onOpen: (task: TaskRow) => void }) {
  const groups = groupTasks(rows);

  // The "next" task (DOSSIER §3.2 #5): live outlines ONE card in orange but never says how that
  // card was chosen — INFERENCE, pinned here: the newest TO DO by `created_at`, ties broken by
  // id (the endpoint orders `(created_at DESC, id DESC)`, so a higher id is newer). A TO DO row
  // with no parseable `created_at` cannot be ranked and simply carries no outline.
  const todo = groups.find((group) => group.key === "todo");
  const ranked = (todo?.rows ?? [])
    .filter((row) => row.created_at !== null && row.created_at !== undefined && !Number.isNaN(Date.parse(row.created_at)))
    .sort((a, b) => {
      const byDate = Date.parse(b.created_at ?? "") - Date.parse(a.created_at ?? "");
      return byDate !== 0 ? byDate : b.id - a.id;
    });
  const nextId = ranked.length > 0 ? ranked[0].id : null;

  return (
    <div className="grid grid-cols-1 gap-[14px] lg:grid-cols-2">
      {groups
        .filter((group) => group.alwaysVisible || group.rows.length > 0)
        .map((group) => (
          <TaskColumn key={group.key} group={group} nextId={nextId} onOpen={onOpen} />
        ))}
    </div>
  );
}

function TaskColumn({
  group,
  nextId,
  onOpen,
}: {
  group: Group;
  nextId: number | null;
  onOpen: (task: TaskRow) => void;
}) {
  // Collapse/expand per column (tasks.md §3.2): the disclosure glyph toggles the body and the
  // count stays visible in BOTH states. Live evidence only shows the expanded state, so the
  // collapsed contract is ours and is pinned here.
  const [collapsed, setCollapsed] = useState(false);
  const bodyId = useId();
  return (
    <section data-group={group.key} className="min-w-0">
      <header className="flex items-center gap-2 border-b border-line pb-1">
        <button
          type="button"
          aria-expanded={!collapsed}
          aria-controls={collapsed ? undefined : bodyId}
          onClick={() => setCollapsed((current) => !current)}
          className="ui-label flex items-center gap-1 text-ink"
        >
          <span aria-hidden>{group.glyph}</span>
          <span>{group.label}</span>
        </button>
        <span className="ui-label ml-auto text-ink-muted">{group.rows.length}</span>
      </header>
      {collapsed ? null : (
        <div id={bodyId} className="mt-2 max-h-[70vh] space-y-[14px] overflow-y-auto pr-1">
          {group.rows.length === 0 ? (
            <p className="ui-label text-ink-muted">None</p>
          ) : (
            group.rows.map((task) => (
              <TaskCard
                key={task.id}
                task={task}
                isNext={group.key === "todo" && task.id === nextId}
                onOpen={onOpen}
              />
            ))
          )}
        </div>
      )}
    </section>
  );
}

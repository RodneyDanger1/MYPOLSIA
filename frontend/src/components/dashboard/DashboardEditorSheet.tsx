"use client";

import { useEffect, useRef } from "react";

import {
  AREA_LABEL,
  AREAS,
  areaOrder,
  layoutStorageAvailable,
  moveWidget,
  restoreDefaultLayout,
  toggleWidget,
  useDashboardLayout,
  WIDGETS,
} from "@/components/dashboard/dashboardLayout";
import { Switch, cn } from "@/components/ui";

/**
 * The Edit Dashboard sheet (wave V12-G1) — the live product's dashboard editor.
 *
 * The original (help.polsia.com/add-dashboard-capabilities, fetched 2026-10-15):
 * `EDIT` in the navbar opens the editor, each widget has a show/hide switch,
 * widgets can be reordered, and "Restore default layout" restores the starting
 * selection and order. This sheet is those controls against the real layout
 * store (`dashboardLayout.ts`) — every switch and arrow mutates the store the
 * dashboard reads, and the effect is visible immediately behind the sheet.
 *
 * Differences, stated in the sheet's own copy rather than hidden: the layout
 * persists browser-local (no server API stores one yet — HANDOFF H1), and if
 * localStorage is unavailable the sheet says the changes last until reload.
 */
export function DashboardEditorSheet({ open, onClose }: { open: boolean; onClose: () => void }) {
  const layout = useDashboardLayout();
  const closeRef = useRef<HTMLButtonElement>(null);

  // Move focus into the sheet on open; return it to whatever opened the sheet
  // (the topbar EDIT button) on close — Esc must never strand the keyboard user.
  useEffect(() => {
    if (!open) return;
    const previous = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    closeRef.current?.focus();
    return () => previous?.focus();
  }, [open]);

  if (!open) return null;

  return (
    <div
      className="fixed inset-0 z-50 flex items-start justify-center bg-black/40 p-4 pt-16"
      role="presentation"
      onMouseDown={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-label="Edit Dashboard"
        data-testid="dashboard-editor"
        className="panel-pop max-h-[75vh] w-[460px] overflow-y-auto"
        onKeyDown={(e) => {
          if (e.key === "Escape") {
            e.stopPropagation();
            onClose();
          }
        }}
      >
        <div className="flex items-center justify-between border-b border-line px-3 py-2">
          <p className="ui-label text-ink">Edit Dashboard</p>
          <button
            ref={closeRef}
            type="button"
            className="btn btn-bevel"
            aria-label="Close edit dashboard"
            data-testid="dashboard-editor-close"
            onClick={onClose}
          >
            ✕
          </button>
        </div>

        <p className="px-3 pt-3 text-xs text-ink-muted">
          Widgets can be shown, hidden and reordered. The layout is saved in this browser only —
          no server API stores a dashboard layout yet, so it does not sync anywhere else.
        </p>
        {!layoutStorageAvailable() ? (
          <p
            role="status"
            data-testid="dashboard-editor-storage-note"
            className="px-3 pt-2 text-xs text-ink-muted"
          >
            Local storage is unavailable in this browser — changes last until you reload.
          </p>
        ) : null}

        {AREAS.map((area) => (
          <section key={area} aria-label={AREA_LABEL[area]} className="px-3 py-3">
            <p className="ui-label text-ink-muted">{AREA_LABEL[area]}</p>
            <ul>
              {areaOrder(layout, area).map((id, index, arr) => {
                const meta = WIDGETS.find((w) => w.id === id);
                const label = meta?.label ?? id;
                const hidden = layout.hidden.includes(id);
                return (
                  <li key={id} data-editor-widget={id} className="flex items-center gap-2 py-1.5">
                    <span className={cn("mr-auto text-sm text-ink", hidden && "text-ink-muted")}>
                      {label}
                    </span>
                    {hidden ? <span className="ui-label text-ink-muted">Hidden</span> : null}
                    <button
                      type="button"
                      className="btn btn-bevel"
                      aria-label={`Move ${label} up`}
                      disabled={index === 0}
                      onClick={() => moveWidget(id, -1)}
                    >
                      ↑
                    </button>
                    <button
                      type="button"
                      className="btn btn-bevel"
                      aria-label={`Move ${label} down`}
                      disabled={index === arr.length - 1}
                      onClick={() => moveWidget(id, 1)}
                    >
                      ↓
                    </button>
                    <Switch
                      checked={!hidden}
                      onChange={() => toggleWidget(id)}
                      label={hidden ? `Show ${label}` : `Hide ${label}`}
                    />
                  </li>
                );
              })}
            </ul>
          </section>
        ))}

        <div className="border-t border-line px-3 py-3">
          <button
            type="button"
            className="btn btn-bevel"
            data-testid="dashboard-editor-restore"
            onClick={restoreDefaultLayout}
          >
            Restore default layout
          </button>
        </div>
      </div>
    </div>
  );
}

"use client";

import { useState } from "react";
import Link from "next/link";
import { Pencil } from "lucide-react";

import { DashboardEditorSheet } from "@/components/dashboard/DashboardEditorSheet";
import { MenuNav } from "@/components/layout/MenuNav";
import { NotificationsBell } from "@/components/layout/NotificationsPanel";

/**
 * The app shell's control cluster.
 *
 * The live product's header row is `workspace name · [🔔] [EDIT] [MENU ▾]` on the
 * light canvas (agent1-live-chrome/DOSSIER.md §1.2, §1.3; agent2-code/CAPTURES.md
 * cap-01). This is that cluster, and it is the ONLY navigation the shell renders —
 * the persistent sidebar is retired (DOSSIER cross-cutting A: the live app has no
 * nav rail; aid3 MATCH-VS-BEAT C2 forbids re-adding one).
 *
 * The bell is real as of wave U2: it reads `GET /api/v1/notifications` (the backend
 * unblocker this wave landed — `backend/app/api/v1/notifications.py`), shows the
 * endpoint's count when it measured one, and owns its three states (loading /
 * failed / answered) in `NotificationsPanel.tsx`. Before that endpoint existed this
 * control refused with a named idle state; it no longer has anything to refuse.
 *
 * EDIT is real as of wave V12-G1: it opens the Edit Dashboard sheet
 * (`components/dashboard/DashboardEditorSheet.tsx`) — per-widget show/hide,
 * within-column reorder, and Restore default layout, the controls the live
 * editor documents (help.polsia.com/add-dashboard-capabilities, fetched
 * 2026-10-15). The layout persists browser-local (`dashboardLayout.ts`); no
 * server API stores a dashboard layout yet, the sheet states that in its own
 * copy, and server persistence is HANDOFF H1 of
 * wave-v12/g1-product/GAP-LEDGER.md §6. Before this wave the button refused by
 * name; it no longer has anything to refuse.
 */
export function Topbar() {
  return (
    <header className="sticky top-0 z-40 border-b border-line bg-surface">
      <div className="mx-auto flex w-full max-w-[1600px] items-center gap-2 px-4 py-2">
        <Link
          href="/dashboard"
          className="mr-auto flex items-baseline gap-2"
          aria-label="Polsia — dashboard"
        >
          <span className="text-base font-bold text-ink">Polsia</span>
          <span className="ui-label text-ink-muted">AI Business Agent</span>
        </Link>

        <NotificationsBell />

        <EditDashboardButton />

        <MenuNav />
      </div>
    </header>
  );
}

/** EDIT — opens the real Edit Dashboard sheet (V12-G1). */
function EditDashboardButton() {
  const [open, setOpen] = useState(false);

  return (
    <>
      <button
        type="button"
        className="btn btn-bevel"
        aria-label="Edit dashboard"
        aria-haspopup="dialog"
        aria-expanded={open}
        onClick={() => setOpen(true)}
        data-testid="topbar-edit"
      >
        <span aria-hidden="true" className="flex items-center">
          <Pencil size={13} />
        </span>
        Edit
      </button>
      <DashboardEditorSheet open={open} onClose={() => setOpen(false)} />
    </>
  );
}

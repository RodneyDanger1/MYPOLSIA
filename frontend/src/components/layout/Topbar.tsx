"use client";

import Link from "next/link";
import { Pencil } from "lucide-react";

import { MenuNav } from "@/components/layout/MenuNav";
import { NotificationsBell } from "@/components/layout/NotificationsPanel";
import { TopbarPopover } from "@/components/layout/TopbarPopover";

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
 * EDIT still refuses by name: the live `EDIT` opens the Edit Dashboard sheet, which
 * is not built yet, and no API stores a dashboard layout (`backend/app/schemas/
 * company.py` carries company fields only) — so it explains that rather than
 * opening a dead sheet.
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

        <TopbarPopover
          label="Edit dashboard"
          testId="topbar-edit"
          icon={<Pencil size={13} />}
          text="Edit"
          panelTitle="Edit Dashboard"
          panelClassName="w-80"
        >
          <p>
            The Edit Dashboard sheet does not exist yet, and no API stores a dashboard layout:
            GET/PUT /api/v1/config carries company fields only. There is nothing to toggle here
            yet.
          </p>
        </TopbarPopover>

        <MenuNav />
      </div>
    </header>
  );
}

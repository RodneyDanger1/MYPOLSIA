"use client";

import type { ReactNode } from "react";

import { usePopover } from "@/components/layout/usePopover";
import { cn } from "@/components/ui/cn";

/**
 * A topbar control that opens a small panel: trigger on the left of a relative
 * wrapper, white `panel-pop` card right-aligned beneath it — the shape of the
 * live product's bell panel (agent2-code/CAPTURES.md cap-04: `NOTIFICATIONS`
 * header in mono caps with a hairline under it, items below, panel anchored
 * under the bell and right-aligned).
 *
 * The trigger is a real button with `aria-expanded`/`aria-controls`; the panel
 * body is whatever honest content the caller has.
 *
 * WHY THIS IS HERE WITH NO CALLER (S2-F2, wave v13 sweep; labelled wave v14):
 * DEAD SURFACE — retired shell popover. It is exported but imported by nothing:
 * the surfaces it was built for now own their own markup — the bell
 * (`NotificationsBell`, components/layout/NotificationsPanel.tsx:224, mounted by
 * Topbar.tsx:49) and the topbar edit sheet (`DashboardEditorSheet`) each render
 * their own trigger + panel. It is kept deliberately (no deletion in a bug-fix
 * wave) so the retire-vs-rewire call is made in the open, not by attrition:
 * see `wave-v14/v3-fe-wire/REQUESTS.md` (R-V14-1) for the decision, owner = the
 * product owner via the wave's decision sheet. Do NOT wire a second popover
 * implementation next to `usePopover`'s live consumers; either delete this
 * component outright or re-point the live surfaces to it — do not do both.
 */
export type TopbarPopoverProps = {
  /** Accessible name for the trigger (the visible content may be an icon alone). */
  label: string;
  /** Trigger icon. */
  icon: ReactNode;
  /** Optional visible trigger text (rendered in the mono caps voice). */
  text?: string;
  /** Panel heading, e.g. `Notifications`. */
  panelTitle: string;
  /** Extra classes for the panel (width lives here). */
  panelClassName?: string;
  testId: string;
  children: ReactNode;
};

export function TopbarPopover({
  label,
  icon,
  text,
  panelTitle,
  panelClassName,
  testId,
  children,
}: TopbarPopoverProps) {
  const { open, toggle, triggerRef, panelRef, panelId, wrapperProps } = usePopover();

  return (
    <div className="relative" {...wrapperProps}>
      <button
        ref={triggerRef}
        type="button"
        className="btn btn-bevel"
        aria-label={label}
        aria-expanded={open}
        aria-controls={panelId}
        onClick={toggle}
        data-testid={testId}
      >
        <span aria-hidden="true" className="flex items-center">
          {icon}
        </span>
        {text ? text : null}
      </button>

      {open ? (
        <div
          ref={panelRef}
          id={panelId}
          className={cn("panel-pop absolute right-0 z-50 mt-1", panelClassName)}
        >
          <p className="ui-label border-b border-line px-3 py-2 text-ink">{panelTitle}</p>
          <div className="px-3 py-3 text-sm text-ink">{children}</div>
        </div>
      ) : null}
    </div>
  );
}

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

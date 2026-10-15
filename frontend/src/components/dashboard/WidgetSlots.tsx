"use client";

import type { ReactNode } from "react";

import {
  areaOrder,
  useDashboardLayout,
  type AreaId,
  type WidgetId,
} from "@/components/dashboard/dashboardLayout";

/**
 * Layout-driven mounting for the dashboard regions (wave V12-G1).
 *
 * `WidgetStack` renders one column's widgets in the owner's saved order and drops
 * the hidden ones; `WidgetSlot` does the same for the two full-height regions
 * (activity feed / chat rail), rendering the whole labelled region or nothing.
 * Both are client components whose children are created by the (server) dashboard
 * page and passed through — so every widget keeps its own file, its own states and
 * its own read; this layer only decides WHERE and WHETHER it mounts.
 *
 * A hidden widget is unmounted, not CSS-hidden: it makes no request, costs
 * nothing while off, and a hidden surface cannot quietly render a stale value.
 * The wrapper divs are the direct children of the column, so the column's
 * `space-y-6` spacing is unchanged from the pre-editor composition.
 */
export function WidgetStack({
  area,
  widgets,
}: {
  area: AreaId;
  widgets: Partial<Record<WidgetId, ReactNode>>;
}) {
  const layout = useDashboardLayout();

  return (
    <>
      {areaOrder(layout, area)
        .filter((id) => !layout.hidden.includes(id))
        .map((id) => (
          <div key={id} data-widget-id={id}>
            {widgets[id]}
          </div>
        ))}
    </>
  );
}

export function WidgetSlot({
  id,
  label,
  className,
  children,
}: {
  id: WidgetId;
  label: string;
  className?: string;
  children: ReactNode;
}) {
  const layout = useDashboardLayout();

  if (layout.hidden.includes(id)) return null;

  return (
    <aside aria-label={label} className={className} data-widget-id={id}>
      {children}
    </aside>
  );
}

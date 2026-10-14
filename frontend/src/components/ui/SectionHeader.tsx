import type { ReactNode } from "react";

import { cn } from "@/components/ui/cn";

/**
 * Section heading in the live product's voice: serif title, optional mono-caps
 * kicker above it, a hairline under the row, and an optional right-hand action
 * (`SEE ALL TASKS`, `VIEW ALL (6)` — agent2-code/CAPTURES.md cap-01).
 *
 * Presentational and hook-free: usable from server components.
 */
export type SectionHeaderProps = {
  /** Heading text, serif. */
  title: ReactNode;
  /** Mono-caps kicker above the heading. */
  kicker?: ReactNode;
  /** Right-hand action slot (a link or a small button). */
  action?: ReactNode;
  className?: string;
};

export function SectionHeader({ title, kicker, action, className }: SectionHeaderProps) {
  return (
    <div className={cn("flex items-end gap-2 border-b border-line pb-1", className)}>
      <div className="mr-auto">
        {kicker ? <p className="ui-label text-ink-muted">{kicker}</p> : null}
        <h2 className="text-lg font-bold leading-tight text-ink">{title}</h2>
      </div>
      {action}
    </div>
  );
}

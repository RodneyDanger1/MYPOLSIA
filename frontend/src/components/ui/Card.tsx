import type { HTMLAttributes, ReactNode } from "react";

import { cn } from "@/components/ui/cn";

/**
 * White card on the canvas — the live product's panel species: white fill, 1px
 * hairline, small radius, serif title with a mono-caps kicker and an optional
 * right-hand action (agent2-code/CAPTURES.md cap-01: `Documents`, `Meta Ads`,
 * `Media` widgets; DOSSIER §G "panels/cards").
 *
 * `title`/`kicker`/`action` are optional — a bare `<Card>` is just the surface,
 * for consumers that bring their own header.
 */
export type CardProps = HTMLAttributes<HTMLElement> & {
  /** Panel heading, in the serif display voice. */
  title?: ReactNode;
  /** Mono-caps kicker above the title. */
  kicker?: ReactNode;
  /** Right-hand slot on the title row (links, buttons). */
  action?: ReactNode;
};

export function Card({ title, kicker, action, className, children, ...props }: CardProps) {
  const hasHeader = Boolean(title || kicker || action);
  return (
    <section className={cn("card-surface", className)} {...props}>
      {hasHeader ? (
        <header className="flex items-end gap-2 border-b border-line px-3 py-2">
          <div className="mr-auto">
            {kicker ? <p className="ui-label text-ink-muted">{kicker}</p> : null}
            {title ? <h2 className="text-base font-bold leading-tight text-ink">{title}</h2> : null}
          </div>
          {action}
        </header>
      ) : null}
      <div className="px-3 py-3">{children}</div>
    </section>
  );
}

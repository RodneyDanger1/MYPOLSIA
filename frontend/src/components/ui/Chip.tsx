import type { HTMLAttributes } from "react";

import { cn } from "@/components/ui/cn";

/**
 * Small mono-ALL-CAPS chip — the live product's most repeated label unit:
 * `FEATURE` / `RESEARCH` on task cards, `2D AGO` age chips, the dark-red `NEW`
 * marker (agent1-live-chrome/DOSSIER.md §1.4, §1.8; CAPTURES.md cap-05).
 *
 * Variants (fill + label pairs are chosen for ≥ 4.5:1; see `globals.css`):
 * - `soft`   — light-gray fill, muted ink (`2D AGO`);
 * - `dark`   — black fill, white label (`FEATURE`, `RESEARCH`);
 * - `accent` — orange fill, near-black label (orange is a fill token; white on
 *              orange is only 3.58:1, so the label is `--accent-ink`);
 * - `alert`  — dark-red fill, white label (`NEW`, destructive).
 *
 * Presentational and hook-free, so it is usable from server components too.
 */
export type ChipVariant = "soft" | "dark" | "accent" | "alert";

export type ChipProps = HTMLAttributes<HTMLSpanElement> & {
  variant?: ChipVariant;
};

const VARIANT_CLASS: Record<ChipVariant, string> = {
  soft: "chip-soft",
  dark: "chip-dark",
  accent: "chip-accent",
  alert: "chip-alert",
};

export function Chip({ variant = "soft", className, children, ...props }: ChipProps) {
  return (
    <span data-variant={variant} className={cn("chip", VARIANT_CLASS[variant], className)} {...props}>
      {children}
    </span>
  );
}

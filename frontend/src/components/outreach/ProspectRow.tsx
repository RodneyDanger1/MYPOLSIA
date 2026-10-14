import type { HTMLAttributes } from "react";

import { Chip, type ChipVariant } from "@/components/ui";
import { cn } from "@/components/ui/cn";
import { absoluteStamp, relativeTime } from "@/components/dashboard/format";

/**
 * One row of `GET /outreach/prospects` (`ProspectResponse`, `app/schemas/social.py`) and its row
 * renderer — the live product's inset-list grammar (glyph/primary text + right-aligned relative
 * date; DOSSIER §5/§6), not the borderless 5-column table the page used to paint.
 *
 * Every optional field is nullable on the wire (`0001`'s `CREATE TABLE prospects` has
 * `server_default`s but no `NOT NULL`), so a missing value renders the app-wide honest
 * placeholder — the em dash / "date unknown" — never an empty cell, never `Invalid Date`, never
 * the string `null`. `last_name` and `title` are carried here because the wire serves them and
 * the old page-local type dropped them.
 *
 * Deliberately action-free: there is no route that sends mail to a prospect on demand, so no
 * per-row send/contact control is rendered (refused by omission; the page's docstring names why).
 */
export type Prospect = {
  id: number;
  email: string;
  first_name: string | null;
  last_name: string | null;
  company: string | null;
  /** Job title — rendered only as the row's `title` attribute (social.md §OPEN DECISION 3's
   *  recommendation: `company` in the visible line, `title` on hover). */
  title: string | null;
  /** Where the prospect came from (`agent`, `manual`, …) — real wire field, kept in the type so
   *  a future surface can render it; not shown on the row (density). */
  source: string | null;
  /** Open string on the wire (`str | None`): the five words below are frontend vocabulary, not a
   *  backend guarantee — anything else renders neutral with its raw word, null renders `—`. */
  status: string | null;
  created_at: string | null;
};

type ChipTreatment = { variant: ChipVariant; className: string };

/**
 * The five vocab words, each with its own treatment (mono-caps chip; text colours are Tailwind
 * 800-step so every hue stays ≥ 4.5:1 on the chip's light fill, the U1 widget convention):
 * `converted` is the one terminal-success state and takes the black-fill chip.
 */
const NAMED_STATUS_TREATMENT: Record<string, ChipTreatment> = {
  new: { variant: "soft", className: "text-blue-800" },
  contacted: { variant: "soft", className: "text-amber-800" },
  replied: { variant: "soft", className: "text-green-800" },
  converted: { variant: "dark", className: "" },
  unsubscribed: { variant: "soft", className: "text-red-800" },
};

/** Null status = "not recorded": the em dash in a gray, dashed chip — distinct from every named
 *  treatment (no colour shared, Q-25's idiom). */
const NULL_TREATMENT: ChipTreatment = {
  variant: "soft",
  className: "border-dashed border-line-strong bg-transparent text-ink-muted",
};

/** Unknown non-null status: neutral chip carrying its raw word, never recast into a known one. */
const OTHER_TREATMENT: ChipTreatment = {
  variant: "soft",
  className: "border-line-strong bg-surface text-ink-muted",
};

/** The five known values each render their own treatment; unknown renders neutral; null renders
 *  the dash (lookup with gray fallback — no binary colouring, no empty chip). */
export function ProspectStatusChip({
  status,
  className,
  ...props
}: { status: string | null } & HTMLAttributes<HTMLSpanElement>) {
  if (status === null) {
    return (
      <Chip variant={NULL_TREATMENT.variant} data-status-tone="unmeasured" className={cn(NULL_TREATMENT.className, className)} {...props}>
        —
      </Chip>
    );
  }
  const named = NAMED_STATUS_TREATMENT[status];
  if (named) {
    return (
      <Chip variant={named.variant} data-status-tone={status} className={cn(named.className, className)} {...props}>
        {status}
      </Chip>
    );
  }
  return (
    <Chip variant={OTHER_TREATMENT.variant} data-status-tone="other" className={cn(OTHER_TREATMENT.className, className)} {...props}>
      {status}
    </Chip>
  );
}

/** `first_name`/`last_name` joined with a space; both missing renders the em dash. */
export function prospectName(prospect: Pick<Prospect, "first_name" | "last_name">): string {
  const parts = [prospect.first_name, prospect.last_name]
    .map((part) => (part ?? "").trim())
    .filter((part) => part !== "");
  return parts.length > 0 ? parts.join(" ") : "—";
}

export function ProspectRow({ prospect }: { prospect: Prospect }) {
  return (
    <li
      data-testid="prospect-row"
      className="flex items-center gap-3 border-b border-line px-3 py-2.5 last:border-b-0"
    >
      <div className="min-w-0 flex-1">
        <p className="truncate font-mono text-sm text-ink" title={prospect.email}>
          {prospect.email}
        </p>
        {/* name · company — one muted line; a missing side renders the em dash, never an empty
            slot. The job title rides the line's `title` attribute (real field, served). */}
        <p className="truncate text-xs text-ink-muted" title={prospect.title ?? undefined}>
          {prospectName(prospect)} · {prospect.company ?? "—"}
        </p>
      </div>
      <ProspectStatusChip status={prospect.status} data-testid="prospect-status" />
      {/* Live-family relative age (`2d ago`), exact stamp on hover; a null/unparseable date keeps
          the app-wide "date unknown" — never `Invalid Date` (Q-10's idiom, kept). */}
      <span
        data-testid="prospect-age"
        className="ui-label whitespace-nowrap text-ink-muted"
        title={prospect.created_at ? absoluteStamp(prospect.created_at) : undefined}
      >
        {relativeTime(prospect.created_at)}
      </span>
    </li>
  );
}

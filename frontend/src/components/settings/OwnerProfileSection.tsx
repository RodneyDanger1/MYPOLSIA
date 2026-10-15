"use client";

/**
 * V15/W1 (4-06, decision #5 LOCKED) — the Owner Profile intake, rendered INSIDE the Company tab
 * panel of the ONE Settings form card.
 *
 * Every field here edits a page-owned STRING draft and rides the page's existing single
 * `PUT /config` (only-changed-keys) — this component fires no request, reads nothing on mount,
 * and owns no state of its own.
 *
 * The "not set" semantics are the wire's own (proven against the shipping
 * `backend/app/schemas/company.py`):
 *   - `owner_skills` / `owner_goal` / `risk_tolerance` / `market_locale` are NOT NULL text at
 *     rest with `''` as the canonical "not set": an empty box sends `''` — one representation,
 *     never `null` against a NOT NULL column;
 *   - `owner_hours_per_week` / `owner_budget_usd` are honestly NULLABLE: an empty box sends JSON
 *     `null`, which stores NULL — "no value". NEVER a fabricated 0: "hours unknown" and
 *     "zero hours" are different facts, and the UI must not invent the second from the first;
 *   - `channels_ready` is a real list on the wire, edited here comma-separated; blank = `[]`
 *     ("none marked ready").
 *
 * Validation messages are rendered from the page's OWN issue map (`issues` prop) — the same
 * drafts the Save guard reads — so the message on screen and the disabled button can never
 * disagree.
 */

export const OWNER_FIELD_KEYS = [
  "owner_skills",
  "owner_hours_per_week",
  "owner_budget_usd",
  "owner_goal",
  "risk_tolerance",
  "channels_ready",
  "market_locale",
] as const;

export type OwnerFieldKey = (typeof OWNER_FIELD_KEYS)[number];

/** The two owner NUMERICS (V15/W1 4-06): they ride text drafts like the morning hour box and share
 *  its blank/null rule. Lives HERE, not in the page module: Next's App-Router page-export validator
 *  allows a page file only its page fields (+ erased types), so a value export from
 *  `app/settings/page.tsx` would fail the production build. */
export const OWNER_NUMERIC_KEYS: readonly OwnerFieldKey[] = [
  "owner_hours_per_week",
  "owner_budget_usd",
];

/** One rendered owner field: the wire key, the label, and the helper copy. The two numerics
 *  declare `inputMode=numeric` (a text input with numeric intent — a `type=number` box would
 *  silently blank non-numeric text, the exact reason the morning-hour box is a text draft too). */
type OwnerFieldSpec = {
  key: OwnerFieldKey;
  label: string;
  helper: string;
  numeric?: boolean;
};

const OWNER_FIELDS: readonly OwnerFieldSpec[] = [
  {
    key: "owner_skills",
    label: "Owner Skills",
    helper:
      "What you bring to this business, in your own words (free text). Blank = not set — the agents then assume nothing about your skills.",
  },
  {
    key: "owner_hours_per_week",
    label: "Owner Hours Per Week",
    numeric: true,
    helper:
      "Whole hours you can work on this business per week (for example 20). Blank = not set (stored as no value) — never 0.",
  },
  {
    key: "owner_budget_usd",
    label: "Owner Budget (USD)",
    numeric: true,
    helper:
      "Budget you can put behind this business, in USD (digits and an optional decimal point; no thousands separator). Blank = not set — never $0.",
  },
  {
    key: "owner_goal",
    label: "Owner Goal",
    helper:
      "The outcome you are working toward (free text). Blank = not set.",
  },
  {
    key: "risk_tolerance",
    label: "Risk Tolerance",
    helper:
      "How much risk you will take, free text (for example conservative / balanced / aggressive). Blank = not set.",
  },
  {
    key: "channels_ready",
    label: "Channels Ready",
    helper:
      "Channels you already have ready to use, comma-separated (for example email, seo). Blank = none marked ready.",
  },
  {
    key: "market_locale",
    label: "Market Locale",
    helper:
      "The market/locale this business serves (for example US-IL or en-US). Blank = not set.",
  },
];

/** The live's measured field density (DOSSIER §2.2: a 49 px-tall edit) — identical to the page's
 *  INPUT_CLASS; kept as its own const so this component stays self-contained. */
const INPUT_CLASS =
  "mt-1 h-[49px] w-full border border-line-strong bg-surface px-3 text-sm text-ink";

export function OwnerProfileSection({
  drafts,
  issues,
  onChange,
}: {
  drafts: Record<OwnerFieldKey, string>;
  issues: Partial<Record<OwnerFieldKey, string | null>>;
  onChange: (key: OwnerFieldKey, value: string) => void;
}) {
  return (
    <div className="space-y-4 border-t border-line pt-4" data-testid="settings-owner-profile">
      <div>
        <p className="ui-label text-ink">Owner Profile</p>
        <p className="ui-label mt-1 text-ink-muted">
          What the agents know about you, the operator. A blank box is stored as “not set” — a
          blank is never saved as a fabricated 0. Saved with the rest of the form (one PUT).
        </p>
      </div>
      {OWNER_FIELDS.map((field) => {
        const id = `settings-field-${field.key}`;
        const issue = issues[field.key] ?? null;
        const helperId = `${id}-helper`;
        const errorId = issue ? `${id}-error` : undefined;
        return (
          <div key={field.key}>
            <label htmlFor={id} className="ui-label block text-ink-muted">
              {field.label}
            </label>
            <input
              id={id}
              type="text"
              inputMode={field.numeric ? "numeric" : undefined}
              autoComplete="off"
              value={drafts[field.key]}
              onChange={(e) => onChange(field.key, e.target.value)}
              aria-invalid={issue ? true : undefined}
              aria-describedby={[helperId, errorId].filter(Boolean).join(" ") || undefined}
              className={issue ? `${INPUT_CLASS} border-alert` : INPUT_CLASS}
            />
            <p id={helperId} className="ui-label mt-1 text-ink-muted">
              {field.helper}
            </p>
            {issue ? (
              <p id={errorId} className="mt-1 text-sm text-alert">
                {issue}
              </p>
            ) : null}
          </div>
        );
      })}
    </div>
  );
}

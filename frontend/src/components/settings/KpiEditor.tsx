"use client";

import { useEffect, useRef, useState } from "react";

import { ApiError, api } from "@/lib/api";
import { Button, SectionHeader, cn } from "@/components/ui";

/**
 * WAVE V23 / L2 — Settings → Business KPIs: the owner's own measurements, correctable in-product.
 *
 * The root of the fixed D1: `company_config.kpis` is a free-form dict served by `GET /config` and
 * rendered by the dashboard's Business panel, but until this component there was no editor — the
 * seeded zeros were the only readings the owner could ever see.  The wire contract this component
 * obeys (backend read-only, proven in `schemas/company.py` + `api/v1/config.py`):
 *
 *  - `PUT /config {"kpis": {...}}` copies only the provided top-level keys (`exclude_unset`), so
 *    this editor sends exactly ONE key — `kpis` — and never rewrites the rest of the form;
 *  - the V22 honesty law: a BLANK box REMOVES that key from the object.  It is never sent as `0`,
 *    because `0` is a real measurement and an absent key is not; the dashboard then renders `—`;
 *  - the SERVER's response is the only state after a save — there is no optimistic update, so a
 *    value the server clamps/rejects is what the form shows, not what was typed;
 *  - a refusal is named inline (the house `ApiError.display` idiom) and the form survives it;
 *  - non-numeric text is refused client-side BEFORE the PUT (`Number("twelve")` is NaN, which
 *    `JSON.stringify` would turn into `null` — a silent clear, the exact dishonesty this wave
 *    exists to remove).  No invented bounds: a huge finite number is the server's call;
 *  - no browser storage, no console, no request on mount — the page's single `GET /config` seeds it.
 */

/** The six keys the dashboard's Business panel reads (`BusinessPanel.tsx`). */
export const KPI_KEYS = [
  "mrr_usd",
  "active_customers",
  "churn_rate",
  "cac_usd",
  "ltv_usd",
  "nps",
] as const;
export type KpiKey = (typeof KPI_KEYS)[number];

/** The wire shape of `company_config.kpis`: free-form, every value unknown until inspected. */
export type KpiValues = Record<string, unknown>;

type KpiRowSpec = { key: KpiKey; label: string; helper: string };

const KPI_ROWS: readonly KpiRowSpec[] = [
  {
    key: "mrr_usd",
    label: "MRR (USD)",
    helper: "Monthly recurring revenue in dollars, as you measured it.",
  },
  {
    key: "active_customers",
    label: "Active Customers",
    helper: "The number of paying customers you count as active today.",
  },
  {
    key: "churn_rate",
    label: "Churn Rate (%)",
    helper: "The churn percentage you measured — the dashboard prints this number with a % sign.",
  },
  {
    key: "cac_usd",
    label: "CAC (USD)",
    helper: "Customer acquisition cost in dollars.",
  },
  {
    key: "ltv_usd",
    label: "LTV (USD)",
    helper: "Customer lifetime value in dollars.",
  },
  {
    key: "nps",
    label: "NPS",
    helper: "Net Promoter Score as you measured it (a negative score is a real reading).",
  },
];

/** Live's measured field density (the Settings page's own `INPUT_CLASS`). */
const INPUT_CLASS =
  "mt-1 h-[49px] w-full border border-line-strong bg-surface px-3 text-sm text-ink";

/** The house error idiom: a refusal shows its code, message and fix hint (`ApiError.display`). */
function errorText(e: unknown): string {
  if (e instanceof ApiError) return e.display;
  if (e instanceof Error) return e.message;
  try {
    return JSON.stringify(e) ?? String(e);
  } catch {
    return String(e);
  }
}

/** A wire `kpis` value as an editable draft. Only finite numbers and strings are readings this
 *  numeric editor can show; anything else (null, an object, a poisoned non-finite number) reads as
 *  an EMPTY box — never the word "null" and never a fabricated 0. */
function draftsFromKpis(kpis: KpiValues | null | undefined): Record<KpiKey, string> {
  const source: KpiValues =
    kpis !== null && typeof kpis === "object" && !Array.isArray(kpis) ? kpis : {};
  const drafts = {} as Record<KpiKey, string>;
  for (const key of KPI_KEYS) {
    const value = source[key];
    if (typeof value === "number" && Number.isFinite(value)) drafts[key] = String(value);
    else if (typeof value === "string") drafts[key] = value;
    else drafts[key] = "";
  }
  return drafts;
}

/** The stored reading as the note prints it: a finite number or a non-blank string is itself;
 *  every absent/unreadable value is the em dash — never 0. */
function storedText(value: unknown): string {
  if (typeof value === "number" && Number.isFinite(value)) return String(value);
  if (typeof value === "string" && value.trim() !== "") return value;
  return "—";
}

/** Plain decimal forms only (optional sign, decimal point, scientific exponent). `Number("")` is
 *  0 and `Number("0x10")` is 16 — both would launder a non-measurement into a reading, so the
 *  accepted grammar is explicit. Thousands separators are refused rather than guessed at. */
const NUMBER_PATTERN = /^[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?$/;

/** `null` = the draft is saveable. Blank is always saveable: it REMOVES the key. */
function kpiIssue(label: string, draft: string): string | null {
  const raw = draft.trim();
  if (raw === "") return null;
  if (!NUMBER_PATTERN.test(raw) || !Number.isFinite(Number(raw))) {
    return `Enter a number for ${label} — digits with an optional sign and decimal point (no thousands separator) — or leave the box blank to remove the key.`;
  }
  return null;
}

/** Exactly the measurements the drafts describe: blank keys are ABSENT (never 0). */
function kpisFromDrafts(drafts: Record<KpiKey, string>): Record<string, number> {
  const body: Record<string, number> = {};
  for (const key of KPI_KEYS) {
    const raw = drafts[key].trim();
    if (raw === "") continue; // blank = key REMOVED — the V22 honesty law
    body[key] = Number(raw);
  }
  return body;
}

export function KpiEditor({
  kpis,
  onSaved,
}: {
  /** The `kpis` object from the page's single `GET /config` (null/absent = nothing measured). */
  kpis: KpiValues | null | undefined;
  /** Called with the server's own `kpis` after a successful save, so the page's config state stays
   *  truthful (optional and side-effect free beyond that). */
  onSaved?: (next: KpiValues) => void;
}) {
  const [drafts, setDrafts] = useState<Record<KpiKey, string>>(() => draftsFromKpis(kpis));
  const [baseline, setBaseline] = useState<Record<KpiKey, string>>(() => draftsFromKpis(kpis));
  /** What the SERVER last served — the note's only source. Editing a box never rewrites it. */
  const [stored, setStored] = useState<KpiValues>(() =>
    kpis !== null && typeof kpis === "object" && !Array.isArray(kpis) ? kpis : {},
  );
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);

  const mounted = useRef(true);
  const savedTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      if (savedTimer.current !== null) {
        clearTimeout(savedTimer.current);
        savedTimer.current = null;
      }
    };
  }, []);

  const issues = {} as Record<KpiKey, string | null>;
  for (const row of KPI_ROWS) issues[row.key] = kpiIssue(row.label, drafts[row.key]);
  const invalid = Object.values(issues).some((issue) => issue !== null);
  const dirty = KPI_KEYS.some((key) => drafts[key] !== baseline[key]);
  const anyDraft = KPI_KEYS.some((key) => drafts[key].trim() !== "");
  const hasStored = KPI_KEYS.some((key) =>
    Object.prototype.hasOwnProperty.call(stored, key),
  );
  const canSave = !saving && !invalid && dirty;

  /** The ONE write path: PUT /config with exactly `{kpis: body}`, then reflect the server. */
  const write = async (body: Record<string, number>) => {
    if (saving) return;
    setSaving(true);
    setSaveError(null);
    setSaved(false);
    if (savedTimer.current !== null) {
      clearTimeout(savedTimer.current);
      savedTimer.current = null;
    }
    try {
      // The ONE key this editor owns. `save` never reaches here unchanged; `clearAll` deliberately
      // sends the empty object.
      const updated = (await api.put<{ kpis?: KpiValues | null }>("/config", {
        kpis: body,
      })) as { kpis?: KpiValues | null } | null;
      if (!mounted.current) return;
      // Reflect the SERVER's answer — the typed draft is not trusted as saved state.
      const rawKpis =
        updated !== null && typeof updated === "object" ? updated.kpis : null;
      const serverKpis =
        rawKpis !== null && typeof rawKpis === "object" && !Array.isArray(rawKpis)
          ? rawKpis
          : {};
      const serverDrafts = draftsFromKpis(serverKpis);
      setDrafts(serverDrafts);
      setBaseline(serverDrafts);
      setStored(serverKpis);
      onSaved?.(serverKpis);
      setSaved(true);
      savedTimer.current = setTimeout(() => {
        savedTimer.current = null;
        setSaved(false);
      }, 3000);
    } catch (e: unknown) {
      // The refusal is named inline and the form is left exactly as the operator typed it.
      if (mounted.current) setSaveError(errorText(e));
    } finally {
      if (mounted.current) setSaving(false);
    }
  };

  const save = () => {
    if (invalid || !dirty) return;
    void write(kpisFromDrafts(drafts));
  };

  /** Clear all removes every key immediately through the same PUT (`{kpis: {}}`). The form does
   *  not blank itself optimistically: the server's empty object is what empties the boxes. */
  const clearAll = () => {
    void write({});
  };

  return (
    <section className="card-surface p-4 space-y-4" aria-label="Business KPIs" data-testid="kpi-editor">
      <SectionHeader kicker="OWNER-ENTERED MEASUREMENTS" title="Business KPIs" />
      <p data-testid="kpi-editor-copy" className="text-sm text-ink">
        These six numbers are OWNER-ENTERED measurements — your own recorded readings, not figures
        computed from transactions. Leave a box blank to remove that key entirely: the dashboard
        then shows the em dash for it instead of inventing a 0.
      </p>

      {saved ? (
        <p
          role="status"
          className="border border-line-strong bg-surface-sunk px-3 py-2 text-sm text-ink"
        >
          KPIs saved
        </p>
      ) : null}

      {saveError ? (
        <div role="alert" className="border border-alert bg-surface p-3">
          <p className="ui-label text-alert">KPI save refused</p>
          <p className="mt-1 text-sm text-ink">{saveError}</p>
        </div>
      ) : null}

      <div className="space-y-4">
        {KPI_ROWS.map((row) => {
          const id = `settings-kpi-${row.key}`;
          const helperId = `${id}-helper`;
          const issue = issues[row.key];
          const errorId = issue ? `${id}-error` : undefined;
          return (
            <div key={row.key}>
              <label htmlFor={id} className="ui-label block text-ink-muted">
                {row.label}
              </label>
              <input
                id={id}
                type="text"
                inputMode="decimal"
                autoComplete="off"
                value={drafts[row.key]}
                onChange={(e) =>
                  setDrafts((current) => ({ ...current, [row.key]: e.target.value }))
                }
                aria-invalid={issue ? true : undefined}
                aria-describedby={[helperId, errorId].filter(Boolean).join(" ") || undefined}
                className={cn(INPUT_CLASS, issue && "border-alert")}
              />
              <p id={helperId} className="ui-label mt-1 text-ink-muted">
                {row.helper}
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

      <p data-testid="kpi-editor-stored" className="ui-label text-ink-muted">
        Stored now:{" "}
        {KPI_KEYS.map((key) => `${key} ${storedText(stored[key])}`).join(" · ")}
      </p>

      <div className="flex items-center gap-2 border-t border-line pt-4">
        <Button variant="glossy" onClick={save} disabled={!canSave}>
          {saving ? "Saving…" : "Save KPIs"}
        </Button>
        <Button onClick={clearAll} disabled={saving || !(anyDraft || hasStored)}>
          Clear all KPIs
        </Button>
      </div>
      <p className="ui-label text-ink-muted">
        Clear all removes all six keys in one PUT — it does not wait for Save KPIs. Save KPIs sends
        every non-blank box as one kpis object; a blank box removes its key.
      </p>
    </section>
  );
}

"use client";

import Link from "next/link";
import { useEffect, useRef, useState } from "react";

import { ApiError, api } from "@/lib/api";
import { Button, SectionHeader, cn } from "@/components/ui";

/**
 * WAVE 2 / F-01 — Settings → Company Plan (pricing_model · goals · github_repo · confirmed_idea).
 *
 * THE MEASURED DEFECT (Visuals F-01, P2).  Of the 24 editable controls on `/settings`, NONE touched
 * `pricing_model`, `goals`, `github_repo` or `confirmed_idea` — yet the agents read all four as
 * company context, and the trial showed them quoting the seed SaaS plan and the seed goal back at a
 * candle company ("$49/$149 Starter/Pro", "Hit $10K MRR") because that is what the row held.  An
 * owner using only the screen could not correct the numbers the agents reason from.
 *
 * WHAT THIS COMPONENT IS, HONESTLY:
 *  - `pricing_model` and `goals` are FREE-FORM JSON objects on the wire (`dict[str, Any]` in
 *    `schemas/company.py`), with no schema inside.  A typed "plan editor" would invent a shape the
 *    server does not enforce (the candle trial wrote `skus[]`, the seed wrote `plans[]`).  So each
 *    is edited as its exact JSON, validated client-side (must be a JSON OBJECT — the server's
 *    declared type), and saved through ONE single-key `PUT /config` (`exclude_unset`), exactly the
 *    `KpiEditor` contract.  The stored value is shown read-only beneath the box, from the SERVER's
 *    answer — never from the draft — so "what I typed" and "what is stored" are visually distinct.
 *  - `github_repo` is a plain text field with the same single-key PUT; blank clears it to JSON
 *    `null` (the server's documented "no value" representation), never the literal string "null".
 *  - `confirmed_idea` is READ-ONLY here and labelled as such: the Planner's confirm step owns that
 *    write (`/api/v1/planner/*`), and a second editor would be a second authority over the same
 *    row.  This panel shows what is stored and links to the surface that writes it.
 *
 * No request fires on mount (the page's single `GET /config` seeds every field here — the pinned
 * "opening Settings issues one read" law), no optimistic update, and every refusal is named inline
 * with the house `ApiError.display` idiom.  There is NO cart/checkout/storefront claim: a product
 * list here is agent context and (when it is a `skus`/`plans` list) what the generated site may
 * print as a catalogue — prices only, never costs or margins.
 */

type JsonObject = Record<string, unknown>;

type JsonValue = Record<string, unknown> | null | undefined;

const INPUT_CLASS =
  "mt-1 w-full border border-line-strong bg-surface px-3 py-2 font-mono text-xs leading-relaxed text-ink";

const RETRY =
  " — fix the value and press Save again; nothing was written and the box keeps what you typed.";

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

/** The stored value as the box shows it: pretty JSON, or an EMPTY box for "no value". */
export function prettyJson(value: JsonValue): string {
  if (value === null || value === undefined) return "";
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return "";
  }
}

/** Parse one draft: blank = clear (null); anything else must be a JSON OBJECT. */
export function parseJsonObject(draft: string): { ok: true; value: JsonObject | null } | { ok: false; issue: string } {
  const raw = draft.trim();
  if (raw === "") return { ok: true, value: null };
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch (e) {
    return { ok: false, issue: `This is not valid JSON (${errorText(e)}).` };
  }
  if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) {
    return {
      ok: false,
      issue:
        "This field is a JSON OBJECT of key/value pairs — start with { and end with }. " +
        "An array, a number or a bare string is not the shape the server stores.",
    };
  }
  return { ok: true, value: parsed as JsonObject };
}

/** USD text for a stored selling price; null when the value is not a real number. */
function usd(value: unknown): string | null {
  if (typeof value !== "number" || !Number.isFinite(value)) return null;
  return `$${value.toLocaleString("en-US", {
    minimumFractionDigits: Number.isInteger(value) ? 0 : 2,
    maximumFractionDigits: 2,
  })}`;
}

/** The first selling price a product row carries (never cost/margin — those are internal). */
function sellingPrice(row: JsonObject): string | null {
  for (const key of ["retail", "price", "price_usd", "amount", "unit_price"]) {
    const text = usd(row[key]);
    if (text !== null) return text;
  }
  return null;
}

/** A readable summary of a pricing_model's product list, IF it has one this page recognises.
 *  Purely presentational: it never feeds a write, and an unrecognised shape prints nothing. */
export function productSummary(model: JsonValue): string[] {
  if (!model || typeof model !== "object" || Array.isArray(model)) return [];
  for (const key of ["skus", "plans", "products", "items", "tiers", "packages", "offerings"]) {
    const list = (model as JsonObject)[key];
    if (!Array.isArray(list)) continue;
    const lines: string[] = [];
    for (const item of list) {
      if (!item || typeof item !== "object" || Array.isArray(item)) continue;
      const row = item as JsonObject;
      const name =
        typeof row.name === "string"
          ? row.name
          : typeof row.sku === "string"
            ? row.sku
            : typeof row.title === "string"
              ? row.title
              : null;
      if (!name) continue;
      const price = sellingPrice(row);
      lines.push(price === null ? name : `${name} — ${price}`);
    }
    if (lines.length > 0) return lines;
  }
  return [];
}

/** "2 groups: launch_60d (3 items), kpi_targets (object)" — or "" when there is nothing. */
export function goalsSummary(goals: JsonValue): string {
  if (!goals || typeof goals !== "object" || Array.isArray(goals)) return "";
  const parts: string[] = [];
  for (const [key, value] of Object.entries(goals as JsonObject)) {
    if (Array.isArray(value)) parts.push(`${key} (${value.length} item${value.length === 1 ? "" : "s"})`);
    else if (value !== null && typeof value === "object") parts.push(`${key} (object)`);
    else parts.push(key);
  }
  return parts.length > 0 ? `${parts.length} group${parts.length === 1 ? "" : "s"}: ${parts.join(", ")}` : "";
}

/** Stored-value line: the readable summary when there is one, else "no value". */
function storedLine(value: JsonValue, readable: string): string {
  if (value === null || value === undefined) return "Stored now: no value (the agents read this as unset).";
  return readable ? `Stored now: ${readable}` : "Stored now: an object with no recognisable product/group list.";
}

type JsonFieldProps = {
  id: string;
  label: string;
  helper: string;
  placeholder: string;
  /** The wire key this field owns — the ONLY key its PUT sends. */
  wireKey: "pricing_model" | "goals";
  value: JsonValue;
  /** Readable summary of a stored value ("" = nothing recognisable to say). */
  describe: (value: JsonValue) => string;
};

/** One free-form JSON field: draft -> validate -> single-key PUT -> reflect the server's answer. */
function JsonField({ id, label, helper, placeholder, wireKey, value, describe }: JsonFieldProps) {
  const initial = prettyJson(value);
  const [draft, setDraft] = useState(initial);
  const [baseline, setBaseline] = useState(initial);
  const [stored, setStored] = useState<JsonValue>(value);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const savedTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  // No timer may outlive the component (a save's "SAVED" banner is not worth a torn-down update).
  useEffect(
    () => () => {
      if (savedTimer.current !== null) {
        clearTimeout(savedTimer.current);
        savedTimer.current = null;
      }
    },
    [],
  );

  const parsed = parseJsonObject(draft);
  const issue = parsed.ok ? null : parsed.issue;
  const dirty = draft !== baseline;
  const canSave = !saving && !issue && dirty;

  const save = async () => {
    if (!parsed.ok || !dirty || saving) return;
    setSaving(true);
    setError(null);
    setSaved(false);
    if (savedTimer.current !== null) {
      clearTimeout(savedTimer.current);
      savedTimer.current = null;
    }
    try {
      // ONE key on the wire: `exclude_unset` means the rest of the company row is untouched.
      const updated = (await api.put<Record<string, unknown>>("/config", {
        [wireKey]: parsed.value,
      })) as Record<string, unknown> | null;
      const serverValue = updated !== null && typeof updated === "object" ? updated[wireKey] : null;
      const serverJson = prettyJson(serverValue as JsonValue);
      // Reflect the SERVER's answer — the typed draft is never trusted as stored state.
      setStored(serverValue as JsonValue);
      setDraft(serverJson);
      setBaseline(serverJson);
      setSaved(true);
      savedTimer.current = setTimeout(() => {
        savedTimer.current = null;
        setSaved(false);
      }, 3000);
    } catch (e: unknown) {
      setError(`${errorText(e)}${RETRY}`);
    } finally {
      setSaving(false);
    }
  };

  const helperId = `${id}-helper`;
  const errorId = `${id}-error`;
  const storedId = `${id}-stored`;

  return (
    <div>
      <label htmlFor={id} className="ui-label block text-ink-muted">
        {label}
      </label>
      <textarea
        id={id}
        rows={8}
        spellCheck={false}
        autoComplete="off"
        value={draft}
        placeholder={placeholder}
        onChange={(e) => setDraft(e.target.value)}
        aria-invalid={issue ? true : undefined}
        aria-describedby={[helperId, issue ? errorId : null, storedId].filter(Boolean).join(" ")}
        className={cn(INPUT_CLASS, issue && "border-alert")}
      />
      <p id={helperId} className="ui-label mt-1 text-ink-muted">
        {helper}
      </p>
      {issue ? (
        <p id={errorId} className="mt-1 text-sm text-alert">
          {issue} Nothing was sent.
        </p>
      ) : null}
      <p id={storedId} data-testid={`${id}-stored`} className="ui-label mt-1 text-ink-muted">
        {storedLine(stored, describe(stored))}
      </p>
      <div className="mt-2 flex items-center gap-2">
        <Button size="sm" onClick={() => void save()} disabled={!canSave}>
          {saving ? "Saving…" : `Save ${label}`}
        </Button>
        {saved ? (
          <span role="status" className="ui-label text-green-700">
            SAVED — the server’s answer is shown above
          </span>
        ) : null}
      </div>
      {error ? (
        <p role="alert" data-testid={`${id}-error`} className="mt-1 text-sm text-alert">
          {error}
        </p>
      ) : null}
    </div>
  );
}

/** `github_repo`: one text value, blank clears to JSON null. */
function GithubRepoField({ value }: { value: string | null | undefined }) {
  const initial = typeof value === "string" ? value : "";
  const [draft, setDraft] = useState(initial);
  const [baseline, setBaseline] = useState(initial);
  const [stored, setStored] = useState<string | null>(typeof value === "string" ? value : null);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const dirty = draft !== baseline;
  const canSave = !saving && dirty;

  const save = async () => {
    if (!dirty || saving) return;
    setSaving(true);
    setError(null);
    setSaved(false);
    try {
      const updated = (await api.put<Record<string, unknown>>("/config", {
        // Blank CLEARS the field (the documented nullable "no value"), never the string "null".
        github_repo: draft.trim() === "" ? null : draft.trim(),
      })) as Record<string, unknown> | null;
      const serverValue =
        updated !== null && typeof updated === "object" && typeof updated.github_repo === "string"
          ? updated.github_repo
          : null;
      const serverDraft = serverValue ?? "";
      setStored(serverValue);
      setDraft(serverDraft);
      setBaseline(serverDraft);
      setSaved(true);
    } catch (e: unknown) {
      setError(`${errorText(e)}${RETRY}`);
    } finally {
      setSaving(false);
    }
  };

  return (
    <div>
      <label htmlFor="settings-github-repo" className="ui-label block text-ink-muted">
        GitHub Repo (owner/repo)
      </label>
      <input
        id="settings-github-repo"
        type="text"
        autoComplete="off"
        value={draft}
        placeholder="owner/repo — blank for no value"
        onChange={(e) => setDraft(e.target.value)}
        aria-describedby="settings-github-repo-helper settings-github-repo-stored"
        className={cn(
          "mt-1 h-[49px] w-full border border-line-strong bg-surface px-3 text-sm text-ink",
        )}
      />
      <p id="settings-github-repo-helper" className="ui-label mt-1 text-ink-muted">
        The repository the code-generation agent is allowed to work in. Stored exactly as typed
        (the seed value is a placeholder); leave blank and Save to clear it.
      </p>
      <p
        id="settings-github-repo-stored"
        data-testid="settings-github-repo-stored"
        className="ui-label mt-1 text-ink-muted"
      >
        {stored === null || stored === ""
          ? "Stored now: no value."
          : `Stored now: ${stored}`}
      </p>
      <div className="mt-2 flex items-center gap-2">
        <Button size="sm" onClick={() => void save()} disabled={!canSave}>
          {saving ? "Saving…" : "Save GitHub Repo"}
        </Button>
        {saved ? (
          <span role="status" className="ui-label text-green-700">
            SAVED — the server’s answer is shown above
          </span>
        ) : null}
      </div>
      {error ? (
        <p role="alert" data-testid="settings-github-repo-error" className="mt-1 text-sm text-alert">
          {error}
        </p>
      ) : null}
    </div>
  );
}

/** Read-only view of `confirmed_idea` (written by the Planner) — never an editor here. */
function ConfirmedIdeaView({ value }: { value: JsonValue }) {
  const idea = value && typeof value === "object" && !Array.isArray(value) ? (value as JsonObject) : null;
  const name = typeof idea?.name === "string" ? idea.name : null;
  const oneLiner = typeof idea?.one_liner === "string" ? idea.one_liner : null;
  const buyer = typeof idea?.buyer === "string" ? idea.buyer : null;
  const band = Array.isArray(idea?.price_band) ? idea.price_band : null;
  const bandText =
    band !== null && band.length === 2 ? `${usd(band[0]) ?? "?"}–${usd(band[1]) ?? "?"}` : null;

  return (
    <div className="border-t border-line pt-4" data-testid="settings-confirmed-idea">
      <p className="ui-label text-ink">Confirmed Idea (read-only — written by the Planner)</p>
      {idea === null ? (
        <p className="ui-label mt-1 text-ink-muted">
          No confirmed idea is stored. The Planner&apos;s confirm step writes it; until then the
          planning agents have no chosen idea to work from.
        </p>
      ) : (
        <dl className="mt-2 space-y-1">
          {name ? (
            <div className="flex items-baseline justify-between gap-3">
              <dt className="ui-label text-ink-muted">Idea</dt>
              <dd className="text-sm text-ink">{name}</dd>
            </div>
          ) : null}
          {oneLiner ? (
            <div className="flex items-baseline justify-between gap-3">
              <dt className="ui-label text-ink-muted">One-liner</dt>
              <dd className="text-sm text-ink">{oneLiner}</dd>
            </div>
          ) : null}
          {buyer ? (
            <div className="flex items-baseline justify-between gap-3">
              <dt className="ui-label text-ink-muted">Buyer</dt>
              <dd className="text-sm text-ink">{buyer}</dd>
            </div>
          ) : null}
          {bandText ? (
            <div className="flex items-baseline justify-between gap-3">
              <dt className="ui-label text-ink-muted">Price band</dt>
              <dd className="text-sm text-ink">{bandText}</dd>
            </div>
          ) : null}
        </dl>
      )}
      <p className="ui-label mt-2 text-ink-muted">
        This panel cannot edit it: the Planner owns that write, and a second editor would be a
        second authority over the same field.{" "}
        <Link href="/planner" className="text-link hover:underline">
          Open the Planner
        </Link>{" "}
        to confirm or change the idea.
      </p>
    </div>
  );
}

export function CompanyPlanSection({
  pricingModel,
  goals,
  githubRepo,
  confirmedIdea,
}: {
  pricingModel?: JsonValue;
  goals?: JsonValue;
  githubRepo?: string | null;
  confirmedIdea?: JsonValue;
}) {
  return (
    <section
      className="card-surface space-y-4 p-4"
      aria-label="Company plan"
      data-testid="company-plan-section"
    >
      <SectionHeader kicker="WHAT THE AGENTS READ" title="Pricing, Goals & Repository" />
      <p className="text-sm text-ink">
        Agents read these fields as company context — the trial showed them quoting the stored
        price points and goals back verbatim. They are free-form JSON on the server, so they are
        edited here exactly as stored: one JSON object per box, saved on its own so the rest of
        the company row is never rewritten. A saved product list is a CATALOGUE for the generated
        site (names and selling prices only); there is no cart, checkout or payment in Meridian.
      </p>

      <JsonField
        id="settings-pricing-model"
        label="Pricing Model"
        wireKey="pricing_model"
        value={pricingModel}
        describe={(value) => productSummary(value).join(" · ")}
        helper={
          'JSON object. A product list can live under "plans", "skus", "products", "items", ' +
          '"tiers", "packages" or "offerings" as a list of objects with a name/sku and a ' +
          'selling price ("retail", "price", "price_usd", "amount" or "unit_price"). ' +
          "Clear the box and Save to store no value."
        }
        placeholder={'{\n  "currency": "USD",\n  "skus": [\n    { "name": "Fireside 8 oz", "retail": 28 }\n  ]\n}'}
      />

      <JsonField
        id="settings-goals"
        label="Goals"
        wireKey="goals"
        value={goals}
        describe={goalsSummary}
        helper={
          "JSON object of goal groups — a group is a list of goal strings or another object " +
          "(for example a KPI target block). Clear the box and Save to store no value."
        }
        placeholder={'{\n  "launch_60d": [\n    "Sell 300 units in the first 90 days"\n  ]\n}'}
      />

      <GithubRepoField value={githubRepo} />

      <ConfirmedIdeaView value={confirmedIdea} />
    </section>
  );
}

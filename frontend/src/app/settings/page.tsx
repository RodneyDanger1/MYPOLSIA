"use client";
import { useEffect, useRef, useState } from "react";
import { ApiError, api } from "@/lib/api";
import { CapabilitiesSection } from "@/components/capabilities/CapabilitiesSection";
import { DeepSeekKeySection } from "@/components/settings/DeepSeekKeySection";
import { DataSection } from "@/components/settings/DataSection";
import { EmailsSection, type NotifyKey } from "@/components/settings/EmailsSection";
import { IntegrationsSection } from "@/components/settings/IntegrationsSection";
import { KpiEditor } from "@/components/settings/KpiEditor";
import { NightTaskSection, type NightDraft } from "@/components/settings/NightTaskSection";
import {
  OWNER_FIELD_KEYS,
  OWNER_NUMERIC_KEYS,
  OwnerProfileSection,
  type OwnerFieldKey,
} from "@/components/settings/OwnerProfileSection";
import { PricingSection } from "@/components/settings/PricingSection";
import { SitePackageSection } from "@/components/settings/SitePackageSection";
import { TabStrip, type TabDef } from "@/components/settings/TabStrip";
import { Button, Switch, cn } from "@/components/ui";

/**
 * Wire shape of GET / PUT /config (`CompanyConfigResponse` in the backend).
 *
 * `timezone` / `daily_cycle_hour` are `| null` on purpose: the `company_config` columns carry no
 * `NOT NULL`, the service layer documents a stored NULL as "no value" (never the 6-hour default), and
 * the response model serves that null — the strict `string` / `number` here was an assumption the
 * server does not make. The form renders null as an empty box.
 *
 * The U5 columns (`night_task_*`, `notification_email`, the four `notify_on_*`) are declared
 * OPTIONAL on purpose: this page's test fixtures (and the pre-U5 ones they inherited) declare full
 * `SettingsConfig` literals without these keys, so a REQUIRED key would stop them compiling — while
 * the runtime reads an absent key as exactly what the server's own defaults serve (booleans OFF,
 * hour/email "no value"). The response model always serves all eight.
 *
 * Exported (type-only — erased at build time, invisible to Next's page-export validator) so this
 * page's tests can pin the nullable contract at COMPILE time: a fixture declaring `timezone: null`
 * stops compiling the moment one side drops the null.
 *
 * The server also serves `pricing_model`, `goals`, `kpis` and `github_repo`. `kpis` is edited by
 * `KpiEditor` (V23/L2), which owns its own single-key PUT (`{"kpis": {...}}` — the same
 * `exclude_unset` route); the other three fields still have no editor. This form owns exactly the
 * `FIELD_KEYS` drafts (the original fourteen fields plus the seven V15/W1 owner-profile drafts),
 * the six booleans and the two night-task numbers — and `PUT /config` copies only the provided
 * keys onto the row (`update_config` → `model_dump(exclude_unset=True)`), so fields rendered
 * elsewhere (or nowhere) keep their stored value.
 */
export type SettingsConfig = {
  name: string;
  mission: string | null;
  vision: string | null;
  description: string | null;
  target_market: string | null;
  value_prop: string | null;
  website_url: string | null;
  industry: string | null;
  product_type: string | null;
  timezone: string | null;
  daily_cycle_hour: number | null;
  night_task_enabled?: boolean;
  night_task_limit?: number;
  night_task_hour?: number | null;
  notification_email?: string | null;
  notify_on_completed?: boolean;
  notify_on_failed?: boolean;
  notify_on_awaiting_approval?: boolean;
  notify_on_run_requeued?: boolean;
  /** V8/C5 (wave V8b/L3): the owner's pause flag — `company_config.dispatch_paused`, served by
   *  GET /config and accepted by PUT /config (`backend/app/schemas/company.py:70,137`). While
   *  true, no agent run is dispatched: the gate refuses by name (`app/agents/crew_factory.py:222`,
   *  E_POLICY_DENIED). Optional-and-nullable like the notify booleans on purpose: an older fixture
   *  without the key reads as the server's own OFF, never a crash. */
  dispatch_paused?: boolean | null;
  /** V11/W4 (B13): the CAN-SPAM sender identity — the two fields the compliance refusals point at
   *  ("set sender_legal_name and sender_postal_address … in the company config — Settings ->
   *  Company", `backend/app/services/email_compliance.py:86`). Both are NOT NULL text columns at
   *  rest (server default ''), so the wire serves a string and a cleared box stores ''; absent
   *  reads as the server's own '' like the booleans above (pre-W4 fixtures keep compiling). */
  sender_legal_name?: string | null;
  sender_postal_address?: string | null;
  /** V15/W1 (4-06, #5 LOCKED): the owner-profile columns — `CompanyConfigResponse`/
   *  `CompanyConfigUpdate` in `backend/app/schemas/company.py:61-67,128-134`. The five text
   *  fields (`owner_skills`, `owner_goal`, `risk_tolerance`, `market_locale` + the
   *  comma-edited `channels_ready` list) are NOT NULL at rest with ''/[] canonical — optional
   *  and nullable here so pre-V15 fixtures keep compiling and an absent key reads the server's
   *  own "not set" (the sender-identity rule above). The two numerics are honestly nullable:
   *  `null` means "no value" and renders as an EMPTY box — the page never fabricates a 0. */
  owner_skills?: string | null;
  owner_hours_per_week?: number | null;
  owner_budget_usd?: number | null;
  owner_goal?: string | null;
  risk_tolerance?: string | null;
  channels_ready?: string[] | null;
  market_locale?: string | null;
  /** V23/L2 (COMPLETENESS #10): the owner's KPI readings — a free-form `dict` at rest, editable
   *  through `KpiEditor` below the form card. Optional-and-nullable so pre-V23 fixtures keep
   *  compiling and an absent key reads as "nothing measured" (blank boxes, `—` in the note). */
  kpis?: Record<string, unknown> | null;
};

/** The keys this form edits as TEXT boxes — the eleven original fields plus the notification
 *  address, the two V11/W4 sender-identity fields, and (V15/W1, 4-06) the seven owner-profile
 *  drafts: five free-text fields, the two numerics (text boxes with numeric validation, like the
 *  morning hour) and the comma-separated `channels_ready` list. Booleans ride `Switch`, the
 *  night hour/limit ride `RadioList` — not drafts. */
export type SettingsFieldKey =
  | "name"
  | "mission"
  | "vision"
  | "description"
  | "target_market"
  | "value_prop"
  | "website_url"
  | "industry"
  | "product_type"
  | "timezone"
  | "daily_cycle_hour"
  | "notification_email"
  | "sender_legal_name"
  | "sender_postal_address"
  | "owner_skills"
  | "owner_hours_per_week"
  | "owner_budget_usd"
  | "owner_goal"
  | "risk_tolerance"
  | "channels_ready"
  | "market_locale";

/** One rendered field: the wire key, the uppercase-voice label, and optional helper copy in the
 *  live modal's convention (DOSSIER §2.2/§2.3: small uppercase copy directly under its field). */
type FieldSpec = { key: SettingsFieldKey; label: string; helper?: string };

/**
 * The repo's house error idiom: a refusal shows its code, its message and — when the server supplied
 * one — the fix hint (`ApiError.display`); anything else shows its own message. Before this page
 * adopted it, a refused save was completely invisible: the button flipped back and nothing happened,
 * so "saved" and "refused" looked identical. A non-Error throw is rendered honestly (its JSON when
 * it serializes) rather than laundered into "[object Object]".
 */
function errorText(e: unknown): string {
  if (e instanceof ApiError) return e.display;
  if (e instanceof Error) return e.message;
  try {
    return JSON.stringify(e) ?? String(e);
  } catch {
    return String(e);
  }
}

/** The twelve text fields this form owns (render order is per panel: COMPANY/SCHEDULER/EMAIL
 *  field lists below; this master list drives drafts + the changed-field computation). V11/W4
 *  added the two sender-identity fields (B13) — they ride the identical only-changed-keys PUT.
 *  V15/W1 (4-06) appends the seven owner-profile drafts (`OWNER_FIELD_KEYS`, rendered by
 *  `OwnerProfileSection` inside the COMPANY panel) — same drafts machinery, same PUT. */
const FIELD_KEYS: readonly SettingsFieldKey[] = [
  "name",
  "industry",
  "product_type",
  "website_url",
  "sender_legal_name",
  "sender_postal_address",
  "mission",
  "vision",
  "description",
  "target_market",
  "value_prop",
  "timezone",
  "daily_cycle_hour",
  "notification_email",
  ...OWNER_FIELD_KEYS,
];

/** The six booleans this form sends (Switch-controlled; never stringified on the wire).
 *  `dispatch_paused` (V8b/L3) rides the same machinery as the five U5 toggles. */
const BOOL_KEYS = [
  "night_task_enabled",
  "notify_on_completed",
  "notify_on_failed",
  "notify_on_awaiting_approval",
  "notify_on_run_requeued",
  "dispatch_paused",
] as const;
type BoolKey = (typeof BOOL_KEYS)[number];
type Bools = Record<BoolKey, boolean>;

const TAB_DEFS: readonly TabDef[] = [
  { id: "company", label: "Company Profile" },
  { id: "scheduler", label: "Scheduler" },
  { id: "emails", label: "Emails" },
  { id: "provider", label: "DeepSeek Key" },
];

/** Live's measured field density (DOSSIER §2.2: a 49 px-tall edit); labels are mono ALL-CAPS. */
const INPUT_CLASS =
  "mt-1 h-[49px] w-full border border-line-strong bg-surface px-3 text-sm text-ink";

const COMPANY_FIELDS: readonly FieldSpec[] = [
  {
    key: "name",
    label: "Company Name",
    helper: "Saved with the rest of the form — there is no separate rename step.",
  },
  { key: "industry", label: "Industry" },
  { key: "product_type", label: "Product Type" },
  { key: "website_url", label: "Website URL", helper: "Shown exactly as stored — including any placeholder the seed wrote; edit it here to replace it." },
  {
    key: "sender_legal_name",
    label: "Sender Legal Name",
    helper:
      "Your legal sender identity for outreach email and invoice footers. Compliance refusals name this field (sender_legal_name) until it is set — this is the box they point at.",
  },
  {
    key: "sender_postal_address",
    label: "Sender Postal Address",
    helper:
      "The physical mailing address a CAN-SPAM footer must carry (sender_postal_address). Same one-PUT save as the rest of the form.",
  },
  { key: "mission", label: "Mission" },
  { key: "vision", label: "Vision" },
  { key: "description", label: "Description" },
  { key: "target_market", label: "Target Market" },
  { key: "value_prop", label: "Value Proposition" },
];

const SCHEDULER_FIELDS: readonly FieldSpec[] = [
  {
    key: "timezone",
    label: "Timezone",
    helper:
      "IANA zone name (for example America/Chicago). The server refuses an unknown zone; clear the box for no value.",
  },
  {
    key: "daily_cycle_hour",
    label: "Morning Cycle Hour (company-local)",
    helper:
      "0–23 in the company-local timezone above, or clear the box for no value — the server stores NULL, not a default.",
  },
];

const EMAIL_FIELDS: readonly FieldSpec[] = [
  {
    key: "notification_email",
    label: "Notification Email",
    helper:
      'Owner notifications are sent here. Leave blank for none — the server stores blank as "not configured". The address is stored as given: the server does not police the format, and the mail provider refuses a malformed one when a notification is attempted.',
  },
];

/** Per-field string drafts — the whole reason the hour box can be CLEARED (Q-18): a numeric state
 *  cannot represent "empty", and a `type=number` box silently blanks non-numeric text (reverting it),
 *  so the hour is a text box with `inputMode=numeric` and its draft is validated explicitly. The
 *  notification address rides the same machinery (blank = "no value"; the server normalizes it). */
type Drafts = Record<SettingsFieldKey, string>;

function draftsFrom(config: SettingsConfig): Drafts {
  const drafts = {} as Drafts;
  for (const key of FIELD_KEYS) {
    // A wire payload can carry an ABSENT key (the type says null, the runtime decides): absent and
    // null both render as an empty box — never the literal string "undefined" or "null".
    const value = config[key] as unknown;
    if (key === "channels_ready") {
      // V15/W1 (4-06): the wire shape is a real list; the box edits it comma-separated. A
      // non-list (absent, null, a hostile payload) reads as "nothing marked ready" — never
      // `String([...])` noise and never a crash.
      drafts[key] = Array.isArray(value)
        ? value.filter((item): item is string => typeof item === "string").join(", ")
        : "";
      continue;
    }
    drafts[key] = value === null || value === undefined ? "" : String(value);
  }
  return drafts;
}

function boolsFrom(config: SettingsConfig): Bools {
  // The bool columns are NOT NULL at rest (server default FALSE); an absent key on the wire (an
  // older fixture) reads the same as the server's own OFF.
  return {
    night_task_enabled: config.night_task_enabled === true,
    notify_on_completed: config.notify_on_completed === true,
    notify_on_failed: config.notify_on_failed === true,
    notify_on_awaiting_approval: config.notify_on_awaiting_approval === true,
    notify_on_run_requeued: config.notify_on_run_requeued === true,
    // V8b/L3: `undefined`/`null` (an older fixture, or a wire hiccup) read as false — the same
    // rule the server's own ON/OFF default keeps; the strict === never renders a coerced truth.
    dispatch_paused: config.dispatch_paused === true,
  };
}

function nightFrom(config: SettingsConfig): NightDraft {
  const hour =
    typeof config.night_task_hour === "number" && Number.isInteger(config.night_task_hour)
      ? config.night_task_hour
      : null;
  const limit =
    typeof config.night_task_limit === "number" && Number.isInteger(config.night_task_limit)
      ? config.night_task_limit
      : null;
  return { hour, limit };
}

/** `null` = the draft is saveable. The range (0–23) stays server-enforced (`CompanyConfigUpdate`),
 *  so the server's 422 — which NAMES the field — is the single source of truth for range violations. */
function hourIssue(draft: string): string | null {
  const raw = draft.trim();
  if (raw === "") return null; // a cleared box means "no value", not midnight
  const parsed = Number(raw);
  if (!Number.isFinite(parsed) || !Number.isInteger(parsed)) {
    return "Enter a whole number (the server accepts 0–23), or clear the box for no value.";
  }
  return null;
}

/** V15/W1 (4-06): the owner numerics ride text drafts like the hour box. `null` = saveable; a
 *  cleared box is "no value" (JSON null), and NON-NUMERIC text is refused by name BEFORE the PUT —
 *  `Number("twelve")` is NaN, which `JSON.stringify` would silently turn into null, and an honest
 *  edit must never become a silent clear. Range stays server-enforced (no invented bounds). The
 *  `OWNER_NUMERIC_KEYS` list itself lives in `components/settings/OwnerProfileSection.tsx` — a
 *  page module may not carry value exports (Next page-export validator). */
function ownerIssue(key: SettingsFieldKey, draft: string): string | null {
  const raw = draft.trim();
  if (raw === "") return null; // cleared box = "not set" (server stores NULL / ''), never 0
  if (key === "owner_hours_per_week") {
    const parsed = Number(raw);
    if (!Number.isFinite(parsed) || !Number.isInteger(parsed)) {
      return "Enter a whole number of hours (for example 20), or clear the box for no value.";
    }
  }
  if (key === "owner_budget_usd") {
    const parsed = Number(raw);
    if (!Number.isFinite(parsed)) {
      return "Enter a number for the budget (digits and an optional decimal point — no thousands separator), or clear the box for no value.";
    }
  }
  return null;
}

/** The owner-field issue map the section renders — computed from the SAME drafts the save guard
 *  reads, so the message on screen and the disabled Save button can never disagree. */
function ownerIssuesFrom(drafts: Drafts): Record<OwnerFieldKey, string | null> {
  const issues = {} as Record<OwnerFieldKey, string | null>;
  for (const key of OWNER_FIELD_KEYS) issues[key] = ownerIssue(key, drafts[key]);
  return issues;
}

/** Exactly the fields the operator CHANGED. Untouched fields are not sent, so a stored NULL is never
 *  rewritten as ""; the morning hour is transmitted as a number, or as JSON null when the box was
 *  cleared. Booleans leave as JSON booleans and the night-task numbers as JSON numbers — the server
 *  validates every one of them (422 naming the field) and `exclude_unset` means only these keys land.
 *
 *  V15/W1 (4-06): the owner numerics follow the hour rule (cleared → JSON null; NEVER 0), the owner
 *  free-text fields leave as strings with '' as the canonical "not set", and `channels_ready` is
 *  parsed from the comma-separated draft into a REAL JSON list (blank → []). */
function changedFields(
  drafts: Drafts,
  baseline: Drafts,
  bools: Bools,
  baseBools: Bools,
  night: NightDraft,
  baseNight: NightDraft,
): Record<string, string | number | boolean | null | string[]> {
  const body: Record<string, string | number | boolean | null | string[]> = {};
  for (const key of FIELD_KEYS) {
    if (drafts[key] === baseline[key]) continue;
    if (key === "daily_cycle_hour" || OWNER_NUMERIC_KEYS.includes(key as OwnerFieldKey)) {
      const raw = drafts[key].trim();
      body[key] = raw === "" ? null : Number(raw);
    } else if (key === "channels_ready") {
      body[key] = drafts[key]
        .split(",")
        .map((item) => item.trim())
        .filter((item) => item !== "");
    } else {
      body[key] = drafts[key];
    }
  }
  for (const key of BOOL_KEYS) {
    if (bools[key] !== baseBools[key]) body[key] = bools[key];
  }
  if (night.hour !== baseNight.hour) body.night_task_hour = night.hour;
  if (night.limit !== baseNight.limit) body.night_task_limit = night.limit;
  return body;
}

export default function SettingsPage() {
  const [config, setConfig] = useState<SettingsConfig | null>(null);
  const [drafts, setDrafts] = useState<Drafts | null>(null);
  const [baseline, setBaseline] = useState<Drafts | null>(null);
  const [bools, setBools] = useState<Bools | null>(null);
  const [baseBools, setBaseBools] = useState<Bools | null>(null);
  const [night, setNight] = useState<NightDraft | null>(null);
  const [baseNight, setBaseNight] = useState<NightDraft | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [tab, setTab] = useState("company");

  const savedTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const mounted = useRef(true);

  // The banner timer is cleared on unmount (Q-24): no state update escapes a torn-down page, and the
  // saved flag can never flip after the page is gone.
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

  useEffect(() => {
    let cancelled = false;
    api
      .get<SettingsConfig>("/config")
      .then((data) => {
        if (cancelled) return;
        setConfig(data);
        const serverDrafts = draftsFrom(data);
        setDrafts(serverDrafts);
        setBaseline(serverDrafts);
        const serverBools = boolsFrom(data);
        setBools(serverBools);
        setBaseBools(serverBools);
        const serverNight = nightFrom(data);
        setNight(serverNight);
        setBaseNight(serverNight);
      })
      .catch((e: unknown) => {
        if (!cancelled) setLoadError(errorText(e));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const hourInvalid = drafts ? hourIssue(drafts.daily_cycle_hour) : null;
  // V15/W1 (4-06): the owner-field issues — computed from the same drafts the save reads; any
  // non-null issue disables Save so a NaN can never reach the PUT as a silent clear.
  const ownerIssues = drafts ? ownerIssuesFrom(drafts) : null;
  const ownerInvalid =
    ownerIssues !== null && Object.values(ownerIssues).some((issue) => issue !== null);
  const dirty =
    drafts !== null &&
    baseline !== null &&
    bools !== null &&
    baseBools !== null &&
    night !== null &&
    baseNight !== null &&
    (FIELD_KEYS.some((key) => drafts[key] !== baseline[key]) ||
      BOOL_KEYS.some((key) => bools[key] !== baseBools[key]) ||
      night.hour !== baseNight.hour ||
      night.limit !== baseNight.limit);
  const canSave =
    config !== null &&
    drafts !== null &&
    baseline !== null &&
    dirty &&
    !saving &&
    !hourInvalid &&
    !ownerInvalid;

  const save = async () => {
    if (
      !config ||
      !drafts ||
      !baseline ||
      !bools ||
      !baseBools ||
      !night ||
      !baseNight ||
      saving ||
      hourInvalid ||
      ownerInvalid
    )
      return;
    const body = changedFields(drafts, baseline, bools, baseBools, night, baseNight);
    if (Object.keys(body).length === 0) return; // "nothing changed" is not a write
    setSaving(true);
    setSaveError(null);
    setSaved(false);
    // A new save restarts the banner window: the previous timer must not hide THIS save's banner.
    if (savedTimer.current !== null) {
      clearTimeout(savedTimer.current);
      savedTimer.current = null;
    }
    try {
      const updated = await api.put<SettingsConfig>("/config", body);
      if (!mounted.current) return;
      setConfig(updated);
      const serverDrafts = draftsFrom(updated);
      setDrafts(serverDrafts);
      setBaseline(serverDrafts);
      const serverBools = boolsFrom(updated);
      setBools(serverBools);
      setBaseBools(serverBools);
      const serverNight = nightFrom(updated);
      setNight(serverNight);
      setBaseNight(serverNight);
      setSaved(true);
      savedTimer.current = setTimeout(() => {
        savedTimer.current = null;
        setSaved(false);
      }, 3000);
    } catch (e: unknown) {
      if (!mounted.current) return;
      setSaveError(errorText(e));
    } finally {
      if (mounted.current) setSaving(false);
    }
  };

  const heading = (
    <div className="border-b border-line pb-2">
      <h1 className="text-3xl font-bold text-ink">Settings</h1>
    </div>
  );

  // Q-24: "not read yet" is a skeleton with `aria-busy`, never the bare word "Loading…".
  if (loading) {
    return (
      <div className="max-w-2xl space-y-4">
        {heading}
        <div aria-busy="true" className="space-y-2">
          <p className="ui-label text-ink-muted">Loading company configuration…</p>
          {[0, 1, 2].map((key) => (
            <div key={key} className="h-[49px] animate-pulse border border-line bg-surface" />
          ))}
        </div>
      </div>
    );
  }

  // The failed read is NAMED (code + fix hint), and no fake form is drawn over it.
  if (!config || !drafts || !baseline || !bools || !baseBools || !night || !baseNight) {
    return (
      <div className="max-w-2xl space-y-4">
        {heading}
        <div role="alert" className="border border-alert bg-surface p-3">
          <p className="ui-label text-alert">Failed to load config</p>
          {loadError ? <p className="mt-1 text-sm text-ink">{loadError}</p> : null}
        </div>
      </div>
    );
  }

  const fieldRow = (field: FieldSpec) => {
    const id = `settings-field-${field.key}`;
    const isHour = field.key === "daily_cycle_hour";
    const invalid = isHour ? hourIssue(drafts[field.key]) : null;
    const helperId = field.helper ? `${id}-helper` : undefined;
    const errorId = invalid ? `${id}-error` : undefined;
    const describedBy = [helperId, errorId].filter(Boolean).join(" ") || undefined;
    return (
      <div key={field.key}>
        <label htmlFor={id} className="ui-label block text-ink-muted">
          {field.label}
        </label>
        <input
          id={id}
          type="text"
          inputMode={isHour ? "numeric" : undefined}
          autoComplete="off"
          value={drafts[field.key]}
          onChange={(e) =>
            setDrafts((current) =>
              current ? { ...current, [field.key]: e.target.value } : current,
            )
          }
          aria-invalid={invalid ? true : undefined}
          aria-describedby={describedBy}
          className={cn(INPUT_CLASS, invalid && "border-alert")}
        />
        {field.helper ? (
          <p id={helperId} className="ui-label mt-1 text-ink-muted">
            {field.helper}
          </p>
        ) : null}
        {invalid ? (
          <p id={errorId} className="mt-1 text-sm text-alert">
            {invalid}
          </p>
        ) : null}
      </div>
    );
  };

  const notifyValues: Record<NotifyKey, boolean> = {
    notify_on_completed: bools.notify_on_completed,
    notify_on_failed: bools.notify_on_failed,
    notify_on_awaiting_approval: bools.notify_on_awaiting_approval,
    notify_on_run_requeued: bools.notify_on_run_requeued,
  };
  const setNotify = (key: NotifyKey, next: boolean) => {
    setBools((current) => (current === null ? current : { ...current, [key]: next }));
  };

  return (
    <div className="max-w-2xl space-y-4">
      {heading}

      {saved ? (
        <p
          role="status"
          className="border border-line-strong bg-surface-sunk px-3 py-2 text-sm text-ink"
        >
          Settings saved successfully
        </p>
      ) : null}

      {/* A refused PUT used to be invisible (the button flipped back and nothing else happened, so
          "saved" and "refused" looked the same). The banner carries the server's own refusal. */}
      {saveError ? (
        <div role="alert" className="border border-alert bg-surface p-3">
          <p className="ui-label text-alert">Save refused</p>
          <p className="mt-1 text-sm text-ink">{saveError}</p>
        </div>
      ) : null}

      <section className="card-surface">
        <div className="px-4 pt-3">
          <TabStrip
            idPrefix="settings"
            tabs={TAB_DEFS}
            active={tab}
            onSelect={setTab}
            ariaLabel="Settings sections"
          />
        </div>

        <div className="p-4">
          {tab === "company" ? (
            <div
              role="tabpanel"
              id="settings-panel-company"
              aria-labelledby="settings-tab-company"
              className="space-y-4"
            >
              {COMPANY_FIELDS.map((field) => fieldRow(field))}
              {/* V15/W1 (4-06, #5): the owner-profile intake — page-owned drafts, riding the same
                  one-PUT save; fires no request of its own. */}
              <OwnerProfileSection
                drafts={drafts}
                issues={ownerIssues ?? {}}
                onChange={(key, value) =>
                  setDrafts((current) =>
                    current === null ? current : { ...current, [key]: value },
                  )
                }
              />
              {/* V8b/L3: the Pause/Resume Switch — the real dispatch gate (V8/C5). `checked` is
                  `dispatch_paused === true`; the save rides the same one-PUT boolean idiom as the
                  notify toggles above (only a CHANGED key leaves the form). */}
              <div className="flex items-start justify-between gap-3 border-t border-line pt-4">
                <div>
                  <p className="ui-label text-ink">Pause all dispatches</p>
                  <p className="ui-label mt-1 text-ink-muted">
                    While paused, no agent run is dispatched — attempts are refused by name
                    (E_POLICY_DENIED). Resume here the same way.
                  </p>
                </div>
                <Switch
                  checked={bools.dispatch_paused}
                  onChange={(next) =>
                    setBools((current) =>
                      current === null ? current : { ...current, dispatch_paused: next },
                    )
                  }
                  label="Pause all dispatches"
                  id="settings-dispatch_paused"
                />
              </div>
            </div>
          ) : null}

          {tab === "scheduler" ? (
            <div
              role="tabpanel"
              id="settings-panel-scheduler"
              aria-labelledby="settings-tab-scheduler"
              className="space-y-4"
            >
              {SCHEDULER_FIELDS.map((field) => fieldRow(field))}
              <NightTaskSection
                enabled={bools.night_task_enabled}
                hour={night.hour}
                limit={night.limit}
                onEnabledChange={(next) =>
                  setBools((current) =>
                    current === null ? current : { ...current, night_task_enabled: next },
                  )
                }
                onHourChange={(next) =>
                  setNight((current) => (current === null ? current : { ...current, hour: next }))
                }
                onLimitChange={(next) =>
                  setNight((current) => (current === null ? current : { ...current, limit: next }))
                }
              />
            </div>
          ) : null}

          {tab === "emails" ? (
            <div
              role="tabpanel"
              id="settings-panel-emails"
              aria-labelledby="settings-tab-emails"
              className="space-y-4"
            >
              {EMAIL_FIELDS.map((field) => fieldRow(field))}
              <EmailsSection values={notifyValues} onChange={setNotify} />
            </div>
          ) : null}

          {tab === "provider" ? (
            <div
              role="tabpanel"
              id="settings-panel-provider"
              aria-labelledby="settings-tab-provider"
            >
              {/* Mounted only while its tab is open (the live EMAILS tab is lazy-loaded the same
                  way, DOSSIER §2.4): leaving the tab unmounts the section, so the status is re-read
                  fresh on return and an open Remove-confirm cannot survive in a hidden page. */}
              <DeepSeekKeySection />
            </div>
          ) : null}
        </div>

        {tab !== "provider" ? (
          <div className="border-t border-line p-4">
            <Button variant="glossy" className="w-full" onClick={save} disabled={!canSave}>
              {saving ? "Saving…" : "Save Settings"}
            </Button>
            <p className="ui-label mt-2 text-ink-muted">
              Saves the config fields you changed — one PUT for the whole form.
            </p>
          </div>
        ) : null}
      </section>

      {/* V23/L2 (COMPLETENESS #10): the owner's KPI measurements — the root of the fixed D1.
          Seeded from this page's ONE GET /config (it fires no read of its own) and saved through
          its own single-key PUT; a save rebases this page's config with the SERVER's answer. */}
      <KpiEditor
        kpis={config.kpis}
        onSaved={(nextKpis) =>
          setConfig((current) => (current === null ? current : { ...current, kpis: nextKpis }))
        }
      />
      {/* V11/W4: the sections that finish the owner's control panel, BELOW the form card on
          purpose — the pinned u3/u7 suites assert the tab list is exactly the four above and that
          `tabpanel` is singular, so these are plain sections, not tabs. None of them fires a
          request on mount (same tests pin that too): every read/write below is click-triggered. */}
      {/* V26/L2 (F2): the integrations surface — credentials the V25 GitHub/Render adapters
          read, with masked status and a real read-only test. A below-card section, NOT a fifth
          tab: the tab list is pinned at four by u3/u7, and u11 pins that opening Settings issues
          exactly one read, so the panel reads its status only on "Check status". */}
      <IntegrationsSection />
      <DataSection />
      <PricingSection />
      <SitePackageSection />
      {/* V16/W4 — the capabilities entry point (H-C1). A plain below-card link section, NOT a tab
          and NOT a nav item this wave: the tab list is pinned at four by the u3/u7/u11 suites, and
          the nav table belongs to another seat (the full minimal-noise decision is recorded in
          `components/capabilities/CapabilitiesSection.tsx`). Fires NOTHING on mount — the pinned
          "sections below the form card fire no request on mount" law. */}
      <CapabilitiesSection />
    </div>
  );
}

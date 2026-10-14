/**
 * Pure display helpers for the dashboard regions.
 *
 * Every value these helpers touch comes off a real endpoint whose payload is nullable or untyped
 * (`src/lib/api.ts` documents each field): a missing value is "not measured" and renders as the em
 * dash — never as a fabricated `0` or `$0.00` — and a timestamp that is absent or unparseable
 * renders as the same "date unknown" placeholder the tasks page uses. Nothing here invents data.
 */

/** The one "not measured" marker this project uses. */
export const NOT_MEASURED = "—";

/**
 * `DashboardSummary.kpis` is `Record<string, unknown>` on purpose — the API carries whatever the
 * company configured and the type must not pretend otherwise. A scalar renders as it is, a missing
 * value becomes the fallback, and a shape with no single-value reading (an object or array) renders
 * the fallback too: nothing can print one object truthfully in a one-line tile. The previous
 * `String(value)` path laundered objects into the literal "[object Object]" that this comment
 * always claimed to avoid — comment and code now say the same thing (audit Q-22).
 */
export function kpiText(value: unknown, fallback: string = NOT_MEASURED): string | number {
  if (typeof value === "string" || typeof value === "number") return value;
  if (value === null || value === undefined) return fallback;
  if (typeof value === "boolean" || typeof value === "bigint") return String(value);
  return fallback;
}

/** A finite number from a number-or-numeric-string reading, `null` for anything else. */
export function kpiNumber(value: unknown): number | null {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value === "string" && value.trim() !== "") {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : null;
  }
  return null;
}

/** Dollar-prefixed reading; a value that already carries a currency symbol is left alone. */
export function moneyText(value: unknown): string {
  const text = kpiText(value);
  if (typeof text === "number") return `$${text}`;
  if (text === NOT_MEASURED) return NOT_MEASURED;
  const asString = String(text);
  return asString.startsWith("$") ? asString : `$${asString}`;
}

/** Percentage reading; a non-numeric value is "not measured", never `NaN%`. */
export function percentText(value: unknown): string {
  const parsed = kpiNumber(value);
  return parsed === null ? NOT_MEASURED : `${parsed}%`;
}

/**
 * Relative age in the live product's vocabulary ("14s ago", "2d ago"), in the browser's clock.
 * A null/unparseable timestamp is "date unknown"; a future timestamp (clock skew between the
 * server and this browser) says so rather than claiming an age that has not happened.
 */
export function relativeTime(iso: string | null | undefined): string {
  if (!iso) return "date unknown";
  const parsed = Date.parse(iso);
  if (Number.isNaN(parsed)) return "date unknown";
  const seconds = Math.floor((Date.now() - parsed) / 1000);
  if (seconds < 0) return "in the future";
  if (seconds < 60) return `${seconds}s ago`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  return `${Math.floor(hours / 24)}d ago`;
}

const MONTHS = [
  "JAN",
  "FEB",
  "MAR",
  "APR",
  "MAY",
  "JUN",
  "JUL",
  "AUG",
  "SEP",
  "OCT",
  "NOV",
  "DEC",
];

/**
 * Absolute stamp in the live product's fixed format — `2:30 PM · OCT 11, 2026` — rendered from the
 * browser clock in LOCAL time. Deliberately not `toLocaleString()`: the audit's Q-10 notes the
 * locale-dependent output that leaves the format unpinned.
 */
export function absoluteStamp(iso: string | null | undefined): string {
  if (!iso) return "date unknown";
  const when = new Date(iso);
  if (Number.isNaN(when.getTime())) return "date unknown";
  const hours24 = when.getHours();
  const hours12 = hours24 % 12 === 0 ? 12 : hours24 % 12;
  const minutes = String(when.getMinutes()).padStart(2, "0");
  const meridiem = hours24 < 12 ? "AM" : "PM";
  return `${hours12}:${minutes} ${meridiem} · ${MONTHS[when.getMonth()]} ${when.getDate()}, ${when.getFullYear()}`;
}

/**
 * Human label for an agent type. Mirrors the nine entries of the backend's
 * `task_service.VALID_AGENT_TYPES` (the same map `AgentStatusGrid` carries); an unknown type falls
 * back to its own words rather than a placeholder, so a new agent stays readable.
 */
const AGENT_LABELS: Record<string, string> = {
  orchestrator: "Orchestrator",
  business_planning: "Strategy",
  competitor_research: "Research",
  social_media: "Social",
  ads_management: "Ads",
  email_outreach: "Outreach",
  code_generation: "Code",
  customer_support: "Support",
  finance: "Finance",
};

export function agentLabel(agentType: string): string {
  return AGENT_LABELS[agentType] ?? agentType.replace(/_/g, " ");
}

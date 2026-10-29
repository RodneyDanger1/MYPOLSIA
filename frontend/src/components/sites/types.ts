/** The wire shapes of the website builder (`GET/POST /api/v1/sites`).
 *
 *  Wave V26 adds `pages`, `template` and `custom_domain` to a summary row — OPTIONAL on purpose: a
 *  site whose `meta.json` predates V26 genuinely has none of them, and the UI must not fabricate a
 *  link (or a "null" template) for a page/provider that site never had. */
export type SiteSummary = {
  slug: string;
  title: string;
  url: string;
  created_at: string;
  bytes: number;
  /** The pages this site actually has, in order. Absent = V24 site (only `index.html`). */
  pages?: string[];
  template?: string;
  /** A bare hostname recorded with the site.  Still only a RECORD in `meta.json` — the site
   *  service never resolves it — but since V29/L6 the `/sites` row mounts `SiteHealthBadge`, which
   *  asks `GET /api/v1/trust/domain` for a live DNS + certificate verdict.  Absent/null = a local
   *  site, which has no domain to check and makes no call. */
  custom_domain?: string | null;
};

/**
 * F-32 — one public-exposure warning `POST /sites` returns with a created site
 * (bridge STOREFRONT-CONTRACT §3): the company-profile fields the generated `contact.html` prints
 * verbatim, and how to stop it. `[]` when nothing is published.
 */
export type PublishingWarning = {
  code: string;
  message: string;
  fields: string[];
  how_to_fix: string;
};

/** The frozen 201 body — where the site is and how many bytes were written.
 *
 *  F-32 appends `publishing_warnings` (ADDITIVE). Typed as `unknown` on the wire and narrowed by
 *  `sitePublishingWarnings` because a pre-F-32 server omits it entirely — absence is "this server
 *  declared nothing", not an empty claim. */
export type SiteCreated = {
  slug: string;
  url: string;
  bytes: number;
  publishing_warnings?: unknown;
};

/** The 201 body's warnings, narrowed defensively. A malformed entry is DROPPED rather than
 *  rendered as half a warning; an absent/non-list value is `[]` (an older server).
 *
 *  Accepts any body that CARRIES the optional key: the create response (`SiteCreated`) and, since
 *  wave 7 / R-W6-A3, the regenerate response (`SiteRegenerated`) — the rebuild now serves the same
 *  census because it re-publishes every page. The parameter is the structural shape the two share,
 *  so ONE narrowing rule stays authoritative for both call sites. */
export function sitePublishingWarnings(
  created: { publishing_warnings?: unknown } | null | undefined,
): PublishingWarning[] {
  const raw = created?.publishing_warnings;
  if (!Array.isArray(raw)) return [];
  const warnings: PublishingWarning[] = [];
  for (const entry of raw) {
    if (entry === null || typeof entry !== "object" || Array.isArray(entry)) continue;
    const record = entry as Record<string, unknown>;
    if (typeof record.code !== "string" || typeof record.message !== "string") continue;
    warnings.push({
      code: record.code,
      message: record.message,
      fields: Array.isArray(record.fields)
        ? record.fields.filter((field): field is string => typeof field === "string")
        : [],
      how_to_fix: typeof record.how_to_fix === "string" ? record.how_to_fix : "",
    });
  }
  return warnings;
}

/* ── Render deploy (wave V25, lane L3) ─────────────────────────────────────────────────────── */
/** The persisted `last_deploy` record of `meta.json` (backend `DeployRecordResponse`).
 *  `state` is the server's word: `submitted` (Render accepted the build, not live yet),
 *  `deployed` (Render said live), `failed` (with `error`).  A hand-edited partial record is
 *  possible, so `state` is an open string and every other field is nullable. */
export type DeployRecord = {
  at: string | null;
  state: string;
  status: string | null;
  deploy_id: string | null;
  url: string | null;
  error: { code?: string; message?: string; outcome_unknown?: boolean } | null;
};

/** `GET /api/v1/sites/{slug}/deploy`.  `last_deploy: null` means NEVER DEPLOYED (the server
 *  distinguishes that from "could not read", which is a failed request, not a null body). */
export type DeployStateResponse = {
  slug: string;
  configured: boolean;
  missing: string[];
  service_id: string | null;
  last_deploy: DeployRecord | null;
};

/** The exact HTTP call a deploy makes — the credential value is masked server-side (`Bearer …`). */
export type DeployPlan = {
  method: string;
  url: string;
  headers: Record<string, string>;
  body: Record<string, unknown>;
};

/** `POST /api/v1/sites/{slug}/deploy`.  `dry_run: true` is a preview that sent nothing;
 *  otherwise `deploy_id` is the Render id that PROVES the submit, and `state` says whether
 *  Render called it live yet. */
export type DeployResponse = {
  slug: string;
  dry_run: boolean;
  configured: boolean;
  state: string;
  status: string | null;
  deploy_id: string | null;
  url: string | null;
  at: string | null;
  message: string;
  plan: DeployPlan;
};

/* ── the approval half of a LIVE deploy (AC-03/AC-04, backend deploy_service) ────────────────── */
/** `POST /api/v1/approvals` — the mint response (`ApprovalIssueResponse`).
 *
 *  `token` is a BEARER CREDENTIAL: it authorizes exactly one fingerprint for its TTL.  It is used
 *  once to repeat the live deploy and is NEVER rendered, logged or kept in a rendered state field.
 *  `expires_at` is epoch seconds (the server's `int(minted.expires_at)`). */
export type ApprovalMint = {
  token: string;
  action_fingerprint: string;
  expires_at: number;
  ttl_s: number | null;
};

/** The approval the server says this LIVE deploy needs: the fingerprint it computed for the exact
 *  `deploy_payload(slug, service_id)` this site would send.  `message` is the server's own named
 *  refusal, quoted so the operator sees WHY the first Confirm did not deploy. */
export type ApprovalGate = {
  fingerprint: string;
  message: string;
};

/** The named-refusal envelope on an `ApiError`: `detail` (HTTPException shape) or `error`
 *  (MeridianError shape), per `lib/api`'s `refusalFrom`.  `null` when the error carries none. */
function refusalEnvelope(error: unknown): Record<string, unknown> | null {
  const body = (error as { body?: unknown } | null)?.body;
  if (!body || typeof body !== "object") return null;
  const bag = body as { detail?: unknown; error?: unknown };
  const envelope =
    bag.detail !== null && typeof bag.detail === "object"
      ? bag.detail
      : bag.error !== null && typeof bag.error === "object"
        ? bag.error
        : null;
  return envelope as Record<string, unknown> | null;
}

/**
 * The approval fingerprint a REFUSED live deploy demands — or `null`.
 *
 * A live Render deploy is `EXTERNAL_IRREVERSIBLE`, so `deploy_service._authorise_live_deploy`
 * refuses it with `E_POLICY_DENIED` until a single-use token for
 * `fingerprint("external_irreversible", "render", deploy_payload(slug, service_id))` is supplied.
 * The backend puts its OWN computed value in the refusal (`context.fingerprint`, alongside
 * `context.decision == "require_approval"`), which is what this reads: the browser never
 * re-derives a security-relevant hash (a client-side copy of the canonicalisation could drift and
 * mint a token that cannot verify — a lying "approved").
 *
 * `null` covers every other refusal: a sandbox block, the kill switch, an exhausted cap, a missing
 * approval channel, or a refusal that names no fingerprint — none of which a mint can resolve.
 */
export function approvalRefusalFingerprint(error: unknown): string | null {
  const code = (error as { code?: unknown } | null)?.code;
  if (code !== "E_POLICY_DENIED") return null;
  const envelope = refusalEnvelope(error);
  const context = envelope?.context;
  if (!context || typeof context !== "object") return null;
  const fields = context as { decision?: unknown; fingerprint?: unknown };
  if (fields.decision !== "require_approval") return null;
  const fingerprint = fields.fingerprint;
  if (typeof fingerprint !== "string") return null;
  const trimmed = fingerprint.trim();
  return trimmed === "" ? null : trimmed;
}

/** What the create form collects. Blank boxes are omitted from the request body. */
export type SiteDraft = {
  title: string;
  tagline: string;
  /** The chosen preset id, or "" when the operator left the default — omitted from the body so the
   *  server's own default (`classic`) applies rather than a client-side copy of it. */
  template: string;
  /** A bare hostname, or "" when none was typed. Validated before the call, sent verbatim. */
  custom_domain: string;
};

/* ── V26: template presets + the domain field's own validation ─────────────────────────────── */
export type SiteTemplateId = "classic" | "modern" | "bold";

/** The server's default preset (`site_service.DEFAULT_TEMPLATE`). */
export const DEFAULT_TEMPLATE: SiteTemplateId = "classic";

/** The three presets the picker offers — the SAME ids the backend renders (`TEMPLATES`). */
export const TEMPLATE_PRESETS: ReadonlyArray<{
  id: SiteTemplateId;
  label: string;
  note: string;
}> = [
  { id: "classic", label: "Classic", note: "Clean, light, editorial." },
  { id: "modern", label: "Modern", note: "Wide, rounded, teal accent." },
  { id: "bold", label: "Bold", note: "Heavy type, high contrast." },
];

/**
 * The domain field's inline check — the same structural rules the backend enforces, so an obvious
 * mistake is caught before a round trip. `null` means "acceptable" ("" included).
 *
 * This is NOT a DNS check and never claims to be one: the label beside the field says the hostname
 * is recorded, not verified.
 */
export function validateCustomDomain(value: string): string | null {
  const domain = value.trim();
  if (domain === "") return null;
  const bare = "Enter a bare hostname such as example.com";
  if (/[^\x20-\x7e]/.test(value)) return `${bare} — ASCII letters, digits, dots and dashes only.`;
  if (domain.includes("://") || domain.includes("/")) {
    return `${bare} — no http://, no path.`;
  }
  if (/[?#:;@[\]\s]/.test(domain)) {
    return `${bare} — no port, query, fragment, spaces or sign-in.`;
  }
  if (domain.startsWith(".") || domain.endsWith(".")) {
    return `${bare} — no leading or trailing dot.`;
  }
  if (domain.includes("..")) return `${bare} — no empty labels ("..").`;
  if (domain.length > 253) return `${bare} — the whole name is limited to 253 characters.`;
  const labels = domain.split(".");
  if (labels.length < 2) return `${bare} — a domain needs at least two labels.`;
  for (const label of labels) {
    if (label.length === 0) return `${bare} — no empty labels.`;
    if (label.length > 63) return `${bare} — each label is limited to 63 characters.`;
    if (!/^[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?$/.test(label)) {
      return `${bare} — labels use letters, digits and dashes only (no edge dash).`;
    }
  }
  return null;
}

/* ── V27: the owner-managed lifecycle (delete + regenerate) ────────────────────────────────── */
/** `DELETE /api/v1/sites/{slug}` — the site whose directory and `meta.json` were removed.
 *  `deleted: true` is the server's confirmation; a refusal is an `ApiError`, never this shape. */
export type SiteDeleted = {
  slug: string;
  deleted: boolean;
};

/** `POST /api/v1/sites/{slug}/regenerate` — the rebuilt site's record.
 *  `slug`, `title`, `created_at`, `template` and `custom_domain` are preserved by the server;
 *  `bytes` is the real total of the files it just rewrote.
 *
 *  Wave 7 / R-W6-A3: the body also serves `publishing_warnings` (ADDITIVE) — the same census the
 *  create path carries, because a rebuild re-publishes every page. Typed `unknown` and narrowed by
 *  `sitePublishingWarnings`, so a pre-wave-7 server (key absent) reads as "declared nothing"
 *  instead of an invented empty claim. */
export type SiteRegenerated = {
  slug: string;
  url: string;
  bytes: number;
  title: string;
  created_at: string;
  pages: string[];
  template: string;
  custom_domain: string | null;
  publishing_warnings?: unknown;
};

/** The absolute URL for Open/Copy: the API serves a site-root-relative path (`/sites/<slug>/`). */
export function absoluteSiteUrl(url: string): string {
  if (typeof window === "undefined") return url;
  try {
    return new URL(url, window.location.origin).toString();
  } catch {
    return url;
  }
}

/** `4321` -> `4,321 bytes`; a non-measurement is stated as such, never as `0`. */
export function bytesText(bytes: number): string {
  return Number.isFinite(bytes) ? `${bytes.toLocaleString("en-US")} bytes` : "size not recorded";
}

/** One message for a refused call: the server's own code + fix hint when it sent one. */
export function describeError(error: unknown): string {
  if (error instanceof Error && error.message) {
    const code = (error as { code?: unknown }).code;
    const howToFix = (error as { howToFix?: unknown }).howToFix;
    if (typeof code === "string" && code.startsWith("E_") && typeof howToFix === "string" && howToFix) {
      return `${code}: ${error.message} — ${howToFix}`;
    }
    return error.message;
  }
  try {
    return JSON.stringify(error) ?? String(error);
  } catch {
    return String(error);
  }
}

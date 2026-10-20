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
  /** A bare hostname recorded with the site — recorded, NOT verified (no DNS check anywhere). */
  custom_domain?: string | null;
};

/** The frozen 201 body — where the site is and how many bytes were written. */
export type SiteCreated = {
  slug: string;
  url: string;
  bytes: number;
};

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
 *  `bytes` is the real total of the files it just rewrote. */
export type SiteRegenerated = {
  slug: string;
  url: string;
  bytes: number;
  title: string;
  created_at: string;
  pages: string[];
  template: string;
  custom_domain: string | null;
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

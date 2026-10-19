/** The wire shapes of the website builder (`GET/POST /api/v1/sites`). */
export type SiteSummary = {
  slug: string;
  title: string;
  url: string;
  created_at: string;
  bytes: number;
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

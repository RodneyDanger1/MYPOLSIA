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

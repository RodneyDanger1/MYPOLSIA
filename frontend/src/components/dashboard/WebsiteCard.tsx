"use client";

import Link from "next/link";
import { useEffect, useRef, useState } from "react";

import { Button, Card } from "@/components/ui";
import { safeHref } from "@/components/planner/CandidateCard";
import { ApiError, api } from "@/lib/api";

import { PanelError, PanelSkeleton } from "./states";

/**
 * The left rail's `Website` card (wave V8b / BEAT B16).
 *
 * What is really here — and what is deliberately NOT:
 *  - the owner-configured site from `GET /config` (`website_url`, `string | null` — the shape
 *    `app/settings/page.tsx` already reads; a stored NULL is "no value", never a default domain);
 *  - ONE client-measured health read of `GET /health` (`backend/app/api/v1/health.py` — the
 *    no-auth liveness probe answering `{"status": "ok"}`) timed with `performance.now()` around
 *    the await. No polling loop: the app already has a realtime lane, a second loop here would be
 *    a duplicate. The words "healthy" require the actual ok answer — the api client resolves ONLY
 *    on an ok response (`res.ok` guard in `lib/api.ts`), and every other outcome gets honest
 *    wording;
 *  - the two management chips as REFUSE-BY-NAME affordances. This deployment ships no
 *    domain-management and no site-versioning backend (nothing under `backend/app` serves one), so
 *    each chip says so BY NAME and calls nothing — never a dead-looking control, never a pretend
 *    success.
 */

/** The wire slice this card reads from `GET /config` (`CompanyConfigResponse` server-side): the
 *  public site URL. `string | null` — the column carries no NOT NULL and the settings page renders
 *  a stored null as an empty box. */
type WebsiteConfig = { website_url: string | null };

/** The card's health-row state: not read yet (`null`), a resolved ok read, a non-ok ANSWER (the
 *  server replied with a status), or a failed read (no usable answer arrived). */
type Health =
  | { kind: "ok"; ms: number; checkedAt: Date }
  | { kind: "answered"; status: number; checkedAt: Date }
  | { kind: "failed"; message: string; checkedAt: Date };

type ChipKey = "domain" | "versions";

/** The named refusals, one per chip — the capability absence stated as the fact it is. */
const REFUSALS: Record<ChipKey, string> = {
  domain:
    "Refused by name — this deployment ships no domain-management backend; nothing was called.",
  versions:
    "Refused by name — this deployment ships no site-versioning backend; nothing was called.",
};

const NOTE_ID = "website-card-note";

/** "H:MM AM/PM" in the reader's local zone — built explicitly (never a locale call) so the pinned
 *  sentence shape is stable in every environment. */
function checkedTime(d: Date): string {
  const hour24 = d.getHours();
  const hour = hour24 % 12 === 0 ? 12 : hour24 % 12;
  const minute = String(d.getMinutes()).padStart(2, "0");
  return `${hour}:${minute} ${hour24 < 12 ? "AM" : "PM"}`;
}

export function WebsiteCard() {
  const [config, setConfig] = useState<WebsiteConfig | null>(null);
  const [configError, setConfigError] = useState<string | null>(null);
  const [health, setHealth] = useState<Health | null>(null);
  const [note, setNote] = useState<ChipKey | null>(null);
  const noteRef = useRef<HTMLParagraphElement | null>(null);

  useEffect(() => {
    let cancelled = false;
    api
      .get<WebsiteConfig>("/config")
      .then((data) => {
        if (!cancelled) setConfig(data);
      })
      .catch((e: unknown) => {
        if (!cancelled) setConfigError(e instanceof Error ? e.message : String(e));
      });
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    let cancelled = false;
    // ONE read, timed around the await — client-measured network latency, exactly what the reader
    // waited for (the api client's own bounded retries included).
    const startedAt = performance.now();
    api
      .get("/health")
      .then(() => {
        if (cancelled) return;
        setHealth({
          kind: "ok",
          ms: Math.round(performance.now() - startedAt),
          checkedAt: new Date(),
        });
      })
      .catch((e: unknown) => {
        if (cancelled) return;
        // A non-ok HTTP ANSWER arrives as an ApiError carrying the server's status; anything else
        // (transport failure, timeout, abort) has no status to report and is a failed read.
        // The typeof guard mirrors useAutoMode's: suites that factory-mock @/lib/api leave ApiError
        // undefined, and instanceof against undefined throws — a mounted-elsewhere hazard.
        if (typeof ApiError !== "undefined" && e instanceof ApiError && e.status > 0) {
          setHealth({ kind: "answered", status: e.status, checkedAt: new Date() });
        } else {
          setHealth({
            kind: "failed",
            message: e instanceof Error ? e.message : String(e),
            checkedAt: new Date(),
          });
        }
      });
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    // The refusal is the answer a keyboard activation asked for — take focus to it so a keyboard /
    // screen-reader user lands on it instead of hunting for the change.
    if (note !== null) noteRef.current?.focus();
  }, [note]);

  const rawUrl = config?.website_url ?? null;
  // "Non-empty string" means one with content — a whitespace-only value reads as not configured
  // (the settings form stores a cleared box as "no value"). Wave V15/W2 (P2-01, U2-04 class):
  // this value is free-form config, so it may become a rendered `href` ONLY as an absolute
  // http(s) URL — `safeHref` is the repo's one guard for that (imported from CandidateCard,
  // where U2-04 landed it). React 18 does not block `javascript:` URLs, and this app origin
  // holds the owner's API key, so every other value (javascript:, data:, file:, relative,
  // malformed) renders as plain text below. The raw value, never a trimmed rewrite, renders.
  const siteUrl = rawUrl !== null && rawUrl.trim() !== "" ? rawUrl : null;
  const safeSiteUrl = safeHref(siteUrl);
  const configLoading = config === null && configError === null;

  return (
    <Card title="Website" aria-label="Website">
      <div className="space-y-2">
        {/* Row 1 — the site, beside the EDIT link into Settings. */}
        <div className="flex items-start gap-2">
          <div className="min-w-0 flex-1">
            {configLoading ? <PanelSkeleton rows={1} /> : null}
            {!configLoading && configError ? (
              <PanelError label="the website configuration" message={configError} />
            ) : null}
            {!configLoading && !configError ? (
              siteUrl !== null ? (
                safeSiteUrl !== null ? (
                  <a
                    href={safeSiteUrl}
                    target="_blank"
                    rel="noopener noreferrer"
                    title={siteUrl}
                    className="link block truncate font-mono text-xs"
                  >
                    {siteUrl}
                  </a>
                ) : (
                  // A configured value that is not an absolute http(s) URL: shown verbatim as
                  // PLAIN TEXT — reporting the stored value is honest; making it clickable is
                  // the sink. `safeHref` above is the only gate.
                  <p
                    data-testid="website-url-text"
                    title={siteUrl}
                    className="block truncate font-mono text-xs text-ink"
                  >
                    {siteUrl}
                  </p>
                )
              ) : (
                <p className="text-sm text-ink-muted">No website configured — set one in Settings.</p>
              )
            ) : null}
          </div>
          <Link href="/settings" className="link ui-label shrink-0">
            EDIT
          </Link>
        </div>

        {/* Row 2 — the measured health line. One of: dash (not read yet), healthy (ok answer),
            answered STATUS (non-ok answer), failed (no usable answer). */}
        {health === null ? <p className="text-sm text-ink-muted">—</p> : null}
        {health?.kind === "ok" ? (
          <p className="text-sm text-ink-muted">
            App healthy (200 OK, {health.ms} ms) — checked {checkedTime(health.checkedAt)}
          </p>
        ) : null}
        {health?.kind === "answered" ? (
          <p className="text-sm text-ink-muted">
            Health check answered {health.status} — checked {checkedTime(health.checkedAt)}
          </p>
        ) : null}
        {health?.kind === "failed" ? (
          // A REAL error, in the states.tsx alert presentation PanelError uses (role="alert" +
          // the red text-alert token). Only the wording differs: this card's sentence is pinned,
          // so PanelError's "Could not load {label}: {message}" template is not reused — the look
          // is.
          <p role="alert" className="text-sm text-alert">
            App health check failed — {health.message} — checked {checkedTime(health.checkedAt)}
          </p>
        ) : null}

        {/* Row 3 — the two management chips + the ONE shared refusal note. */}
        <div className="flex flex-wrap items-center gap-2 border-t border-line pt-2">
          <Button
            variant="bevel"
            size="sm"
            aria-expanded={note === "domain"}
            aria-controls={NOTE_ID}
            onClick={() => setNote("domain")}
          >
            MANAGE DOMAIN
          </Button>
          <Button
            variant="bevel"
            size="sm"
            aria-expanded={note === "versions"}
            aria-controls={NOTE_ID}
            onClick={() => setNote("versions")}
          >
            VERSIONS
          </Button>
        </div>
        {note !== null ? (
          // Capability absence, NOT a fault: the neutral inline voice (mono small text on the
          // quiet inset surface), never the red alert treatment — red means a real error here.
          <p
            id={NOTE_ID}
            ref={noteRef}
            role="note"
            tabIndex={-1}
            className="ui-label border border-line bg-surface-sunk px-2 py-1 text-ink-muted"
          >
            {REFUSALS[note]}
          </p>
        ) : null}
      </div>
    </Card>
  );
}

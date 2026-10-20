"use client";

import { useEffect, useState } from "react";

import { describeError } from "@/components/sites/types";
import { api } from "@/lib/api";

/**
 * One recorded custom domain's REAL health, from `GET /api/v1/trust/domain`.
 *
 * This is the first thing in the product that actually checks a `custom_domain`: V26 stores the
 * hostname as a RECORD ("recorded, not verified"), and this badge is where that changes — it asks
 * the backend for a Cloudflare DoH A/AAAA lookup plus a live TLS handshake, and shows what came
 * back.  Three honest outcomes, plus the two absences:
 *
 *   * `TLS <n>d`     — the certificate's real remaining days (`title` carries the expiry instant);
 *   * `DNS FAIL`     — the name does not resolve (the DNS `status`/`reason` is in `title`);
 *   * `CHECK FAILED` — the certificate leg failed, the call was refused, or the body was not a
 *                      health result; `title` names the reason.  Never a fabricated `TLS 0d`.
 *   * `CHECKING…`    — the read is in flight (a pending state, not a claim);
 *   * no badge at all when the host is empty — a site with no custom domain has nothing to check,
 *     and `SiteList` renders `LOCAL` for it instead.  No request is made in that case.
 */
type BadgeView = {
  state: "checking" | "dns" | "failed" | "tls";
  label: string;
  title: string;
};

/** Only what the badge renders — narrowed field by field, because the body is a wire value. */
type HealthRead = {
  dnsStatus: number | null;
  dnsReason: string | null;
  tlsOk: boolean;
  daysLeft: number | null;
  notAfter: string | null;
  tlsReason: string | null;
  reason: string | null;
};

function asRecord(value: unknown): Record<string, unknown> | null {
  if (value === null || typeof value !== "object" || Array.isArray(value)) return null;
  return value as Record<string, unknown>;
}

/** A body that is not a domain-health result is a BROKEN READ (null), never a healthy badge. */
function asHealthRead(body: unknown): HealthRead | null {
  const record = asRecord(body);
  const dns = asRecord(record?.dns);
  const tls = asRecord(record?.tls);
  if (record === null || dns === null || tls === null) return null;
  if (typeof tls.ok !== "boolean") return null;
  return {
    dnsStatus: typeof dns.status === "number" ? dns.status : null,
    dnsReason: typeof dns.reason === "string" ? dns.reason : null,
    tlsOk: tls.ok,
    daysLeft: typeof tls.days_left === "number" ? tls.days_left : null,
    notAfter: typeof tls.not_after === "string" ? tls.not_after : null,
    tlsReason: typeof tls.reason === "string" ? tls.reason : null,
    reason: typeof record.reason === "string" ? record.reason : null,
  };
}

const CHECKING: BadgeView = {
  state: "checking",
  label: "CHECKING…",
  title: "Checking the recorded domain (DNS and certificate)…",
};

function viewFrom(read: HealthRead): BadgeView {
  // RCODE 0 is NOERROR.  Anything else (3 = NXDOMAIN, 2 = SERVFAIL, …) is a DNS failure the
  // backend already named, and the certificate cannot exist for a name that does not resolve.
  if (read.dnsStatus !== null && read.dnsStatus !== 0) {
    return {
      state: "dns",
      label: "DNS FAIL",
      title: read.dnsReason ?? read.reason ?? `DNS status ${read.dnsStatus}`,
    };
  }
  if (!read.tlsOk) {
    return {
      state: "failed",
      label: "CHECK FAILED",
      title: read.tlsReason ?? read.reason ?? "the certificate check failed",
    };
  }
  if (read.daysLeft === null) {
    return {
      state: "failed",
      label: "CHECK FAILED",
      title: read.tlsReason ?? "the certificate expiry was not readable",
    };
  }
  if (read.daysLeft < 0) {
    return {
      state: "failed",
      label: "TLS EXPIRED",
      title: read.notAfter
        ? `The certificate expired on ${read.notAfter}`
        : "The certificate has expired",
    };
  }
  return {
    state: "tls",
    label: `TLS ${read.daysLeft}d`,
    title: read.notAfter
      ? `Certificate expires ${read.notAfter}`
      : "The certificate is valid; its expiry was not returned",
  };
}

export function SiteHealthBadge({ host }: { host: string | null | undefined }) {
  const trimmed = typeof host === "string" ? host.trim() : "";
  const [view, setView] = useState<BadgeView>(CHECKING);

  useEffect(() => {
    if (!trimmed) return;
    let cancelled = false;
    setView(CHECKING);
    // `await` (not `.then`) on purpose: a test double or a proxy that answers `undefined` must
    // degrade to CHECK FAILED, never throw inside the effect and take the whole row list down.
    void (async () => {
      try {
        const body = await api.get<unknown>(`/trust/domain?host=${encodeURIComponent(trimmed)}`);
        if (cancelled) return;
        const read = asHealthRead(body);
        if (read === null) {
          setView({
            state: "failed",
            label: "CHECK FAILED",
            title: "the server returned no health result for this domain",
          });
          return;
        }
        setView(viewFrom(read));
      } catch (error: unknown) {
        if (cancelled) return;
        setView({ state: "failed", label: "CHECK FAILED", title: describeError(error) });
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [trimmed]);

  // No host => nothing to claim and nothing to ask: the row shows LOCAL instead.
  if (!trimmed) return null;

  return (
    <span
      data-testid="site-health-badge"
      data-state={view.state}
      data-host={trimmed}
      title={view.title}
      aria-live="polite"
      className="ui-label whitespace-nowrap border border-line bg-surface px-1.5 py-0.5 text-ink-muted"
    >
      {view.label}
    </span>
  );
}

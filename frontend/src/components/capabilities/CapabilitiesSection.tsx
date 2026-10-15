import Link from "next/link";

import { SectionHeader } from "@/components/ui";

/**
 * The capabilities ENTRY POINT (V16/W4 wiring decision, documented in the lane receipt):
 * a below-the-form section on Settings that links to `/capabilities`.
 *
 * The minimal-noise decision, recorded: the nav table (`components/layout/nav-items.ts`) is
 * another writer's file this wave (the phone seat owns it and the shell-count re-pins), and the
 * command palette derives its ROUTES section FROM that table — so a palette/nav entry added here
 * would either cross-write that file or mint a second, hand-kept route list (the exact
 * two-truths defect this campaign kills). Settings is the app's inspection/control home and the
 * plan names it first; the deeper pages already live as below-card sections here (Data, Pricing,
 * Site package). This section fires NO request on mount — it is a plain link; the page itself
 * reads `GET /api/v1/ops/capabilities` only when opened.
 */
export function CapabilitiesSection() {
  return (
    <section className="card-surface p-4" data-testid="settings-capabilities">
      <SectionHeader kicker="read-only · the registry is the truth" title="Capabilities" />
      <p className="mt-2 text-sm text-ink">
        What every agent can actually do — its registry tools with the registry&apos;s own
        descriptions, its allowed action classes and the external channels it may touch. Derived
        live from the policy firewall and the tool registry; nothing on that page is hand-copied.
      </p>
      <Link
        data-testid="settings-capabilities-link"
        className="mt-2 inline-block text-sm underline underline-offset-2"
        href="/capabilities"
      >
        Open the capability map
      </Link>
    </section>
  );
}

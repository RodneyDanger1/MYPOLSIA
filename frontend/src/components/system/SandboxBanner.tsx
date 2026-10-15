"use client";

import { useEffect, useState } from "react";

import { api } from "@/lib/api";

/**
 * App-wide sandbox-mode banner (V11 unit 2, capability audit B1/B2).
 *
 * Reads GET /api/v1/ops/sandbox (backend `app/api/v1/ops.py`, read-only BY DESIGN: the flag is
 * `settings.SANDBOX_MODE`, flipped in `.env` + restart). The shipped default is sandboxed, and
 * until this read nothing outside the API process could say so: every external action was
 * drafted instead of sent, and the UI offered no explanation.
 *
 * The three honest states, in load order:
 *   - `sandbox_mode: true` renders a persistent banner that says it plainly, with the flip
 *     instructions (no hidden state, no surprise at send time);
 *   - `sandbox_mode: false` (or any body that is not literally true) renders NOTHING: live mode is
 *     the unremarkable state and a banner would be noise;
 *   - a FAILED read renders NOTHING. Silence is the honest fallback here, and deliberately so: a
 *     banner fetch failure is not evidence of sandbox mode, and an error strip telling the user
 *     the sandbox read broke would make an app that is working fine look broken. The failure is
 *     not even logged to the DOM; nothing here may look like an app failure.
 *
 * One read per page load, nothing polled (C7). The state it reports is static per process
 * lifetime (`.env` + restart), so there is no freshness claim to make.
 */
export function SandboxBanner() {
  const [sandboxed, setSandboxed] = useState(false);

  useEffect(() => {
    let cancelled = false;
    api
      .get<{ sandbox_mode?: unknown }>("/ops/sandbox")
      .then((body) => {
        // Exactly `true` turns the banner on; a hostile or partial body is not a claim.
        if (!cancelled) setSandboxed(body?.sandbox_mode === true);
      })
      .catch(() => {
        /* Failed read: stay silent by design. See the docstring above. */
      });
    return () => {
      cancelled = true;
    };
  }, []);

  if (!sandboxed) return null;

  return (
    <div role="status" data-testid="sandbox-banner" className="bg-band text-band-ink">
      <div className="mx-auto flex w-full max-w-[1600px] flex-wrap items-baseline gap-x-3 gap-y-1 px-4 py-2">
        <span className="ui-label">Sandbox mode ON</span>
        <p className="text-sm" data-testid="sandbox-banner-copy">
          External actions are drafted, not sent; flip SANDBOX_MODE in .env and restart to go live.
        </p>
      </div>
    </div>
  );
}

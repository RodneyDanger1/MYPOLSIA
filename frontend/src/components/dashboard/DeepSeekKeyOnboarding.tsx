"use client";

import { useEffect, useState } from "react";
import Link from "next/link";

import { Chip } from "@/components/ui";
import { api, type DeepSeekKeyStatus } from "@/lib/api";

/**
 * Dashboard onboarding prompt: the owner sees it on a fresh install until a DeepSeek key
 * is configured, and it auto-hides the moment `configured` becomes true.
 *
 * Behaviour, deliberately boring:
 *  - status unknown (still loading, or the status read failed) → render nothing.  A prompt
 *    that fires on a failed read would be a false alarm; the Settings page always shows
 *    the honest state.
 *  - `configured: false` → the banner, with a link straight to Settings.
 *  - Dismiss → the banner is hidden for this browser from then on (a single boolean in
 *    `localStorage`; NO key material of any kind is ever written client-side).  The prompt
 *    is not a nag: one dismiss is enough, and the Settings page remains the permanent
 *    entry point.
 */

export const ONBOARDING_DISMISS_KEY = "polsia.deepseek_onboarding.dismissed";

export function DeepSeekKeyOnboarding() {
  const [configured, setConfigured] = useState<boolean | null>(null);
  const [dismissed, setDismissed] = useState(false);

  useEffect(() => {
    try {
      setDismissed(window.localStorage.getItem(ONBOARDING_DISMISS_KEY) === "1");
    } catch {
      // Storage unavailable (privacy mode): treat as not dismissed; the hide is session-only.
    }
  }, []);

  useEffect(() => {
    let active = true;
    api
      .get<DeepSeekKeyStatus>("/config/deepseek-key")
      .then((status) => {
        if (active) setConfigured(status.configured);
      })
      .catch(() => {
        if (active) setConfigured(null); // unknown: stay silent rather than guess
      });
    return () => {
      active = false;
    };
  }, []);

  const dismiss = () => {
    setDismissed(true);
    try {
      window.localStorage.setItem(ONBOARDING_DISMISS_KEY, "1");
    } catch {
      // Session-only dismissal is an acceptable degradation.
    }
  };

  if (configured !== false || dismissed) {
    // V9/B5 — the key-OK state gains its next step (plan §9 B5: the "Plan my company" link after
    // the key lands). Additive: the NOT-configured banner below is untouched, and so is the rule
    // that an UNKNOWN status renders nothing at all. Same dismissal, so the pair is never a nag.
    if (configured === true && !dismissed) {
      return (
        <div
          role="status"
          data-testid="planner-onboarding"
          className="card-surface flex items-start justify-between gap-4 px-4 py-3"
        >
          <div>
            <p className="flex items-center gap-2">
              <Chip variant="accent">Ready</Chip>
              <span className="text-sm font-bold text-ink">The agents are switched on</span>
            </p>
            <p className="mt-1 text-sm text-ink-muted">
              A DeepSeek key is configured. The planner can research candidate businesses and draft
              them against your hours and budget — nothing runs from a proposal until you confirm it.
            </p>
            <Link href="/planner" className="link mt-2 inline-block text-xs">
              Plan my company
            </Link>
          </div>
          <button
            onClick={dismiss}
            aria-label="Dismiss"
            className="ui-label text-ink-muted hover:text-ink"
          >
            ×
          </button>
        </div>
      );
    }
    return null;
  }

  return (
    <div
      role="status"
      data-testid="deepseek-onboarding"
      className="card-surface flex items-start justify-between gap-4 px-4 py-3"
    >
      <div>
        <p className="flex items-center gap-2">
          <Chip variant="accent">Setup</Chip>
          <span className="text-sm font-bold text-ink">
            Add a DeepSeek API key to switch the agents on
          </span>
        </p>
        <p className="mt-1 text-sm text-ink-muted">
          No DeepSeek key is configured yet, so agent work that needs the model cannot run. Paste
          your key in Settings — it is stored on the backend and never in this browser.
        </p>
        <Link href="/settings" className="link mt-2 inline-block text-xs">
          Open Settings → AI model (DeepSeek)
        </Link>
      </div>
      <button
        onClick={dismiss}
        aria-label="Dismiss"
        className="ui-label text-ink-muted hover:text-ink"
      >
        ×
      </button>
    </div>
  );
}

"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
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
 *    is not a nag: one dismiss is enough, and the Settings link in the sidebar remains the
 *    permanent entry point.
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

  if (configured !== false || dismissed) return null;

  return (
    <div
      role="status"
      data-testid="deepseek-onboarding"
      className="bg-indigo-900/40 border border-indigo-500 rounded-lg px-4 py-3 flex items-start justify-between gap-4"
    >
      <div>
        <p className="text-indigo-100 text-sm font-medium">
          Add a DeepSeek API key to switch the agents on
        </p>
        <p className="text-indigo-200/80 text-xs mt-1">
          No DeepSeek key is configured yet, so agent work that needs the model cannot run.
          Paste your key in Settings — it is stored on the backend and never in this browser.
        </p>
        <Link
          href="/settings"
          className="text-indigo-300 hover:text-indigo-200 text-xs underline mt-2 inline-block"
        >
          Open Settings → AI model (DeepSeek)
        </Link>
      </div>
      <button
        onClick={dismiss}
        aria-label="Dismiss"
        className="text-indigo-200/70 hover:text-white text-lg leading-none"
      >
        ×
      </button>
    </div>
  );
}

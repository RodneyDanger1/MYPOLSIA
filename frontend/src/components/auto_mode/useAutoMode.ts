"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { ApiError, api } from "@/lib/api";

/** Wire shapes of GET/POST/DELETE /api/v1/auto-mode (`auto_mode.py` :60-94) — consumed verbatim. */
export type AutoModeSession = {
  id: number;
  started_at: string;
  ends_at: string;
  stopped_at: string | null;
  stop_reason: string | null;
  tasks_run: number;
  spend_used: number | null;
  time_left_seconds: number | null;
};

export type AutoModeStatus = { active: boolean; session: AutoModeSession | null };

/**
 * The AUTO MODE panel's one data source (wave V7 / V7-T14).
 *
 * Reads on mount and on a modest interval, and re-seeds from the answer of every action
 * (POST/DELETE return the session itself — no follow-up read is invented). The interval is
 * cleared on unmount: no timer outlives the page, and no state update escapes a torn-down panel.
 */

/** The poll cadence: one honest refresh a minute keeps the counters and the countdown live. */
const REFRESH_MS = 60_000;

/**
 * The house error idiom, kept local so this hook survives suites that mock `@/lib/api` with a
 * restricted factory (some mocks omit `ApiError` entirely — `instanceof undefined` throws).
 */
function errorText(e: unknown): string {
  if (typeof ApiError !== "undefined" && e instanceof ApiError) return e.display;
  if (e instanceof Error) return e.message;
  try {
    return JSON.stringify(e) ?? String(e);
  } catch {
    return String(e);
  }
}

/** A hostile/empty payload is a named failure — never laundered into an "inactive" window. */
function asStatus(data: unknown): AutoModeStatus {
  if (data === null || typeof data !== "object") {
    throw new Error("the endpoint answered without a usable auto-mode status");
  }
  const candidate = data as { active?: unknown; session?: unknown };
  if (typeof candidate.active !== "boolean") {
    throw new Error("the endpoint answered without a usable auto-mode status");
  }
  if (candidate.active && (candidate.session === null || typeof candidate.session !== "object")) {
    throw new Error("the endpoint says a window is active but served no session");
  }
  return {
    active: candidate.active,
    session:
      candidate.session === null || candidate.session === undefined
        ? null
        : (candidate.session as AutoModeSession),
  };
}

export function useAutoMode() {
  const [status, setStatus] = useState<AutoModeStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const [lastStopped, setLastStopped] = useState<AutoModeSession | null>(null);

  const mounted = useRef(true);

  const read = useCallback(async () => {
    try {
      const data = asStatus(await api.get<unknown>("/auto-mode"));
      if (!mounted.current) return;
      setStatus(data);
      setLoadError(null);
    } catch (e: unknown) {
      if (!mounted.current) return;
      setLoadError(errorText(e));
    }
  }, []);

  useEffect(() => {
    mounted.current = true;
    void read().finally(() => {
      if (mounted.current) setLoading(false);
    });
    const timer = setInterval(() => {
      void read();
    }, REFRESH_MS);
    return () => {
      mounted.current = false;
      clearInterval(timer);
    };
  }, [read]);

  const start = useCallback(async (durationHours: number) => {
    setBusy(true);
    setActionError(null);
    try {
      const data = await api.post<{ started: boolean; session: AutoModeSession }>("/auto-mode", {
        duration_hours: durationHours,
      });
      if (!mounted.current) return;
      setLastStopped(null);
      setStatus({ active: true, session: data.session });
    } catch (e: unknown) {
      if (mounted.current) setActionError(errorText(e));
    } finally {
      if (mounted.current) setBusy(false);
    }
  }, []);

  const stop = useCallback(async () => {
    setBusy(true);
    setActionError(null);
    try {
      const data = await api.delete<{ stopped: boolean; session: AutoModeSession }>("/auto-mode");
      if (!mounted.current) return;
      setStatus({ active: false, session: null });
      setLastStopped(data.session);
    } catch (e: unknown) {
      if (mounted.current) setActionError(errorText(e));
    } finally {
      if (mounted.current) setBusy(false);
    }
  }, []);

  return { status, loading, loadError, busy, actionError, lastStopped, start, stop };
}

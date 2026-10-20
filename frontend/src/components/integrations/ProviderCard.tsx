"use client";

import { useEffect, useRef, useState } from "react";

import { ApiError, api } from "@/lib/api";
import { Button, Chip, cn } from "@/components/ui";
import type { ProviderStatus, ProviderTestResult } from "@/components/integrations/types";

/**
 * ONE provider card inside Settings → Integrations (V26/L2).
 *
 * The card renders the SERVER's masked status; it never derives or guesses a verdict.  A
 * credential typed here lives in this component's state for the save round-trip only — no
 * localStorage, no cookie — and the box is cleared the moment the save lands, so the value is
 * never left in the DOM.
 *
 * A read-only provider (stripe / meta_ads / google_ads) renders status only: no input, no
 * Save, no Clear.  Its Test button is still offered because the API's answer is the truth:
 * for a provider with no read-only probe wired it comes back as a NAMED refusal, and that
 * refusal is shown verbatim rather than dressed as a success.
 */

function errorText(e: unknown): string {
  if (e instanceof ApiError) return e.display;
  if (e instanceof Error) return e.message;
  try {
    return JSON.stringify(e) ?? String(e);
  } catch {
    return String(e);
  }
}

function badgeText(configured: boolean): string {
  return configured ? "Configured" : "Missing";
}

export function ProviderCard({
  status,
  onChange,
}: {
  status: ProviderStatus;
  onChange: (next: ProviderStatus) => void;
}) {
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState<"save" | "clear" | "test" | null>(null);
  const [notice, setNotice] = useState<{ text: string; isError: boolean } | null>(null);
  const [testResult, setTestResult] = useState<{ text: string; ok: boolean } | null>(null);
  const [confirmClear, setConfirmClear] = useState(false);

  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  const supplied = Object.entries(drafts)
    .map(([name, value]) => [name, value.trim()] as const)
    .filter(([, value]) => value !== "");
  const canSave = !status.read_only && supplied.length > 0 && busy === null;

  const save = async () => {
    if (!canSave) return;
    setBusy("save");
    setNotice(null);
    setTestResult(null);
    setConfirmClear(false);
    try {
      const next = await api.put<ProviderStatus>(`/integrations/${status.provider}`, {
        values: Object.fromEntries(supplied),
      });
      if (!mounted.current) return;
      onChange(next);
      setDrafts({}); // the value lives here for the round-trip only
      setNotice({ text: "Saved — stored on the backend, never returned by the API.", isError: false });
    } catch (e: unknown) {
      if (!mounted.current) return;
      setNotice({ text: `Could not save. ${errorText(e)}`, isError: true });
    } finally {
      if (mounted.current) setBusy(null);
    }
  };

  const clear = async () => {
    if (busy !== null) return;
    setBusy("clear");
    setNotice(null);
    setTestResult(null);
    setConfirmClear(false);
    try {
      const next = await api.delete<ProviderStatus>(`/integrations/${status.provider}`);
      if (!mounted.current) return;
      onChange(next);
      setDrafts({});
      setNotice({
        text: next.configured
          ? "Stored value removed — the environment value is still active."
          : "Stored value removed. This provider is not configured now.",
        isError: false,
      });
    } catch (e: unknown) {
      if (!mounted.current) return;
      setNotice({ text: `Could not clear. ${errorText(e)}`, isError: true });
    } finally {
      if (mounted.current) setBusy(null);
    }
  };

  const test = async () => {
    if (busy !== null) return;
    setBusy("test");
    setNotice(null);
    setTestResult(null);
    try {
      const result = await api.post<ProviderTestResult>(`/integrations/${status.provider}/test`);
      if (!mounted.current) return;
      setTestResult({ text: `Connected — ${result.detail}`, ok: result.ok });
    } catch (e: unknown) {
      // A refusal is a real answer: render the server's code/message, never "connected".
      if (!mounted.current) return;
      setTestResult({ text: `Test refused — ${errorText(e)}`, ok: false });
    } finally {
      if (mounted.current) setBusy(null);
    }
  };

  return (
    <section
      data-testid={`integration-card-${status.provider}`}
      aria-label={status.label}
      className="space-y-3 border-t border-line pt-3 first:border-t-0 first:pt-0"
    >
      <div className="flex flex-wrap items-center gap-2">
        <h3 className="mr-auto text-sm font-bold text-ink">{status.label}</h3>
        {status.read_only ? (
          <Chip variant="soft" className="whitespace-nowrap">
            Read-only
          </Chip>
        ) : null}
        <Chip
          data-testid={`integration-badge-${status.provider}`}
          variant={status.configured ? "dark" : "alert"}
          className="whitespace-nowrap"
        >
          {badgeText(status.configured)}
        </Chip>
      </div>

      <div className="space-y-2">
        {status.secrets.map((secret) => (
          <div key={secret.name} className="flex flex-wrap items-center gap-2">
            <span className="ui-label w-56 shrink-0 text-ink-muted">{secret.name}</span>
            <code
              data-testid={`integration-masked-${status.provider}-${secret.name}`}
              className="border border-line bg-surface-sunk px-2 py-1 font-mono text-xs text-ink"
            >
              {secret.configured ? secret.masked : "…"}
            </code>
            {status.read_only ? null : (
              <input
                type="password"
                aria-label={secret.name}
                placeholder={secret.configured ? "new value (replaces the stored one)" : "value"}
                autoComplete="off"
                spellCheck={false}
                value={drafts[secret.name] ?? ""}
                onChange={(event) =>
                  setDrafts((current) => ({ ...current, [secret.name]: event.target.value }))
                }
                className="ml-auto h-9 w-64 border border-line-strong bg-surface px-2 text-sm text-ink"
              />
            )}
          </div>
        ))}
      </div>

      {status.read_only ? (
        <p className="ui-label text-ink-muted">
          Set in the environment/.env — this panel reports the status and never owns the value.
        </p>
      ) : null}

      <div className="flex flex-wrap items-center gap-2">
        {status.read_only ? null : (
          <Button variant="glossy" size="sm" onClick={save} disabled={!canSave}>
            {busy === "save" ? "Saving…" : "Save"}
          </Button>
        )}
        {!status.read_only && status.source === "stored" && !confirmClear ? (
          <Button size="sm" onClick={() => setConfirmClear(true)} disabled={busy !== null}>
            Clear stored
          </Button>
        ) : null}
        {!status.read_only && confirmClear ? (
          <span
            role="group"
            aria-label={`Confirm clearing ${status.label}`}
            className="flex flex-wrap items-center gap-2 border border-alert bg-surface p-1"
          >
            <span className="ui-label text-alert">Clear the stored value?</span>
            <Button size="sm" className="text-alert" onClick={clear} disabled={busy !== null}>
              {busy === "clear" ? "Clearing…" : "Confirm clear"}
            </Button>
            <Button size="sm" onClick={() => setConfirmClear(false)} disabled={busy !== null}>
              Cancel
            </Button>
          </span>
        ) : null}
        <Button size="sm" onClick={test} disabled={busy !== null}>
          {busy === "test" ? "Testing…" : "Test connection"}
        </Button>
      </div>

      {notice ? (
        <p
          role={notice.isError ? "alert" : "status"}
          className={cn(
            "border p-2 text-sm",
            notice.isError
              ? "border-alert bg-surface text-alert"
              : "border-line bg-surface-sunk text-ink",
          )}
        >
          {notice.text}
        </p>
      ) : null}

      {testResult ? (
        <p
          role={testResult.ok ? "status" : "alert"}
          data-testid={`integration-test-result-${status.provider}`}
          className={cn(
            "border p-2 text-sm",
            testResult.ok
              ? "border-line bg-surface-sunk text-ink"
              : "border-alert bg-surface text-alert",
          )}
        >
          {testResult.text}
        </p>
      ) : null}
    </section>
  );
}

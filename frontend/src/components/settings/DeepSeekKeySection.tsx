"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError, api, type DeepSeekKeyStatus, type DeepSeekVerifyResult } from "@/lib/api";
import { Button, Chip, cn } from "@/components/ui";

/**
 * Settings → "AI model (DeepSeek)".
 *
 * The key lifecycle the owner asked for: enter a key, have it SAVED (backend-side), CHANGE it,
 * REMOVE it, and TEST it against the live DeepSeek API. The masked value and the source word
 * (`stored` / `env` / `none`) are rendered exactly as the API reports them — this component never
 * guesses a friendlier verdict. The DRAFT key lives in this component's state only, for the save
 * round-trip; it is never persisted client-side (no localStorage, no cookie) and it is cleared as
 * soon as the save lands.
 *
 * Verify semantics: if there is an unsaved draft it probes the draft (verify-before-save);
 * otherwise it probes whatever is configured (stored, else environment). The result shown
 * is the server's real probe outcome — `ok: false` prints the genuine reason (e.g. HTTP
 * 401, timeout) rather than an invented one.
 *
 * Q-19: failures are NOT dressed as neutral statuses — every failure notice renders in the
 * `role="alert"` red idiom, successes stay `role="status"`; and Remove — the one destructive,
 * unrecoverable action here — requires an explicit confirm click before the DELETE fires.
 */

const SOURCE_LABEL: Record<DeepSeekKeyStatus["source"], string> = {
  stored: "Configured (stored in app)",
  env: "Configured via environment",
  none: "Not configured",
};

const SOURCE_VARIANT: Record<DeepSeekKeyStatus["source"], "dark" | "soft" | "alert"> = {
  stored: "dark",
  env: "soft",
  none: "alert",
};

/** The wire type is a closed union, but the UI must not blank out on a word it does not know:
 *  an unrecognised source renders itself, and an absent one says so. */
function sourceLabel(source: string | undefined): string {
  if (source && source in SOURCE_LABEL) return SOURCE_LABEL[source as DeepSeekKeyStatus["source"]];
  return source ? `Configured (source: ${source})` : "Source unknown";
}

function sourceVariant(source: string | undefined): "dark" | "soft" | "alert" {
  if (source && source in SOURCE_VARIANT) return SOURCE_VARIANT[source as DeepSeekKeyStatus["source"]];
  return "soft";
}

/** ApiError carries a `.display` (code + message + fix); anything else shows its own message —
 *  a non-Error throw is rendered honestly rather than laundered into "[object Object]". */
function errorText(e: unknown): string {
  if (e instanceof ApiError) return e.display;
  if (e instanceof Error) return e.message;
  try {
    return JSON.stringify(e) ?? String(e);
  } catch {
    return String(e);
  }
}

type Notice = { text: string; isError: boolean };

export function DeepSeekKeySection() {
  const [status, setStatus] = useState<DeepSeekKeyStatus | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const [editing, setEditing] = useState(false);
  const [busy, setBusy] = useState<"save" | "remove" | "verify" | null>(null);
  const [notice, setNotice] = useState<Notice | null>(null);
  const [verifyResult, setVerifyResult] = useState<DeepSeekVerifyResult | null>(null);
  const [confirmRemove, setConfirmRemove] = useState(false);

  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  const refresh = useCallback(async () => {
    setLoadError(null);
    try {
      const next = await api.get<DeepSeekKeyStatus>("/config/deepseek-key");
      if (!mounted.current) return;
      setStatus(next);
    } catch (e) {
      if (!mounted.current) return;
      setLoadError(errorText(e));
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const save = async () => {
    const value = draft.trim();
    if (!value || busy) return; // the button is disabled too; this guard keeps the key out of a blank PUT
    setBusy("save");
    setNotice(null);
    setVerifyResult(null);
    setConfirmRemove(false);
    try {
      const next = await api.put<DeepSeekKeyStatus>("/config/deepseek-key", { api_key: value });
      if (!mounted.current) return;
      setStatus(next);
      setDraft(""); // the key lives in component state for the save round-trip only
      setEditing(false);
      setNotice({
        text: "Saved. The key is stored on the backend and used by the agent runtime.",
        isError: false,
      });
    } catch (e) {
      if (!mounted.current) return;
      setNotice({ text: `Could not save the key. ${errorText(e)}`, isError: true });
    } finally {
      if (mounted.current) setBusy(null);
    }
  };

  const removeStored = async () => {
    if (busy) return;
    setBusy("remove");
    setNotice(null);
    setVerifyResult(null);
    setConfirmRemove(false);
    try {
      const next = await api.delete<DeepSeekKeyStatus>("/config/deepseek-key");
      if (!mounted.current) return;
      setStatus(next);
      setDraft("");
      setEditing(false);
      setNotice({
        text: next.configured
          ? "Stored key removed. The environment key is still active."
          : "Stored key removed. No DeepSeek key is configured now.",
        isError: false,
      });
    } catch (e) {
      if (!mounted.current) return;
      setNotice({ text: `Could not remove the stored key. ${errorText(e)}`, isError: true });
    } finally {
      if (mounted.current) setBusy(null);
    }
  };

  const verify = async () => {
    if (busy) return;
    const candidate = draft.trim();
    setBusy("verify");
    setNotice(null);
    setVerifyResult(null);
    try {
      const result = await api.post<DeepSeekVerifyResult>(
        "/config/deepseek-key/verify",
        candidate ? { api_key: candidate } : {},
      );
      if (!mounted.current) return;
      setVerifyResult(result);
    } catch (e) {
      // The probe could not even be requested — that is a real failure and is shown as one.
      if (!mounted.current) return;
      setVerifyResult({ ok: false, detail: `Could not run the verification. ${errorText(e)}` });
    } finally {
      if (mounted.current) setBusy(null);
    }
  };

  const configured = status?.configured ?? false;
  const isStored = status?.source === "stored";
  const showInput = status !== null && (editing || !isStored);

  return (
    <div className="space-y-4" data-testid="deepseek-key-section">
      <div className="flex items-start justify-between gap-3">
        <div>
          <p className="ui-label text-ink-muted">Provider</p>
          <h2 className="text-lg font-bold text-ink">AI model (DeepSeek)</h2>
          <p className="mt-1 text-sm text-ink-muted">
            The agents call DeepSeek with this key. It is stored on the backend only, never in
            this browser, and the API never returns it — not even in an error.
          </p>
        </div>
        {status ? (
          <Chip
            data-testid="deepseek-source"
            variant={sourceVariant(status.source)}
            className="whitespace-nowrap"
          >
            {sourceLabel(status.source)}
          </Chip>
        ) : null}
      </div>

      {loadError ? (
        <div role="alert" className="border border-alert bg-surface p-3">
          <p className="text-sm text-alert">Could not load the key status. {loadError}</p>
          <Button size="sm" className="mt-2" onClick={() => void refresh()}>
            Retry
          </Button>
        </div>
      ) : status === null ? (
        // "Not read yet" is a skeleton, not a claim about the key.
        <div aria-busy="true" className="space-y-2">
          <p className="ui-label text-ink-muted">Checking the key status…</p>
          <div className="h-[49px] animate-pulse border border-line bg-surface" />
        </div>
      ) : (
        <div className="space-y-3">
          <div className="flex flex-wrap items-center gap-2">
            <span className="ui-label text-ink-muted">Current key</span>
            <code
              data-testid="deepseek-masked"
              className="border border-line bg-surface-sunk px-2 py-1 font-mono text-xs text-ink"
            >
              {configured ? status.masked || "—" : "…"}
            </code>
          </div>

          {showInput && (
            <label className="block">
              <span className="ui-label block text-ink-muted">
                {isStored ? "New API key (replaces the stored one)" : "API key"}
              </span>
              <input
                type="password"
                value={draft}
                onChange={(e) => setDraft(e.target.value)}
                placeholder="sk-…"
                autoComplete="off"
                spellCheck={false}
                aria-label="DeepSeek API key"
                className="mt-1 h-[49px] w-full border border-line-strong bg-surface px-3 text-sm text-ink"
              />
            </label>
          )}

          <div className="flex flex-wrap items-center gap-2">
            {showInput && (
              <Button
                variant="glossy"
                onClick={save}
                disabled={busy !== null || draft.trim() === ""}
              >
                {busy === "save" ? "Saving…" : "Save key"}
              </Button>
            )}
            {isStored && !editing && !confirmRemove && (
              <Button
                onClick={() => {
                  setEditing(true);
                  setConfirmRemove(false); // a Remove-confirm must not survive a mode change
                  setNotice(null);
                }}
                disabled={busy !== null}
              >
                Change
              </Button>
            )}
            {editing && (
              <Button
                onClick={() => {
                  setEditing(false);
                  setDraft("");
                }}
                disabled={busy !== null}
              >
                Cancel
              </Button>
            )}
            {(configured || draft.trim() !== "") && (
              <Button onClick={verify} disabled={busy !== null}>
                {busy === "verify" ? "Verifying…" : "Verify"}
              </Button>
            )}
            {isStored && !editing && !confirmRemove && (
              <Button
                className="text-alert"
                onClick={() => {
                  setConfirmRemove(true);
                  setNotice(null);
                }}
                disabled={busy !== null}
              >
                Remove
              </Button>
            )}
          </div>

          {/* Q-19: the key cannot be recovered from this app — `status.masked` is all the API ever
              returns — so the destructive action is a confirmed one. Escape cancels, like every
              other confirm surface in this repo. */}
          {isStored && !editing && confirmRemove ? (
            <div
              role="group"
              aria-label="Confirm removing the stored key"
              onKeyDown={(event) => {
                if (event.key === "Escape") {
                  event.preventDefault();
                  setConfirmRemove(false);
                }
              }}
              className="flex flex-wrap items-center gap-2 border border-alert bg-surface p-2"
            >
              <p className="ui-label text-alert">
                Remove the stored key? It cannot be recovered here.
              </p>
              <Button
                className="text-alert"
                onClick={removeStored}
                disabled={busy !== null}
              >
                {busy === "remove" ? "Removing…" : "Remove key"}
              </Button>
              <Button onClick={() => setConfirmRemove(false)} disabled={busy !== null}>
                Cancel
              </Button>
            </div>
          ) : null}
        </div>
      )}

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

      {verifyResult ? (
        verifyResult.ok ? (
          <p
            role="status"
            data-testid="deepseek-verify-result"
            className="border border-line bg-surface-sunk p-2 text-sm text-ink"
          >
            Verified — {verifyResult.detail}
          </p>
        ) : (
          <p
            role="alert"
            data-testid="deepseek-verify-result"
            className="border border-alert bg-surface p-2 text-sm text-alert"
          >
            Not verified — {verifyResult.detail}
          </p>
        )
      ) : null}
    </div>
  );
}

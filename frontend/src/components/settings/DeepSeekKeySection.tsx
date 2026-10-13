"use client";

import { useCallback, useEffect, useState } from "react";
import { api, type DeepSeekKeyStatus, type DeepSeekVerifyResult } from "@/lib/api";

/**
 * Settings → "AI model (DeepSeek)".
 *
 * The key lifecycle the owner asked for: enter a key, have it SAVED (backend-side; the
 * browser keeps the draft in component state only, and only until the save lands), CHANGE
 * it, REMOVE it, and TEST it against the live DeepSeek API.  The masked value and the
 * source label (`stored` / `env` / `none`) are rendered exactly as the API reports them —
 * this component never guesses a friendlier verdict, and it never stores the key anywhere
 * client-side.
 *
 * Verify semantics: if there is an unsaved draft it probes the draft (verify-before-save);
 * otherwise it probes whatever is configured (stored, else environment).  The result shown
 * is the server's real probe outcome — `ok: false` prints the genuine reason (e.g. HTTP
 * 401, timeout) rather than an invented one.
 */

const SOURCE_LABEL: Record<DeepSeekKeyStatus["source"], string> = {
  stored: "Configured (stored in app)",
  env: "Configured via environment",
  none: "Not configured",
};

const SOURCE_BADGE: Record<DeepSeekKeyStatus["source"], string> = {
  stored: "bg-green-900/50 border-green-500 text-green-300",
  env: "bg-indigo-900/50 border-indigo-500 text-indigo-300",
  none: "bg-gray-700 border-gray-500 text-gray-300",
};

/** ApiError carries a `.display` (code + message + fix); anything else falls back safely. */
function errorText(e: unknown): string {
  if (e && typeof e === "object" && "display" in e) {
    const display = (e as { display?: unknown }).display;
    if (typeof display === "string" && display) return display;
  }
  if (e instanceof Error) return e.message;
  return String(e);
}

export function DeepSeekKeySection() {
  const [status, setStatus] = useState<DeepSeekKeyStatus | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const [editing, setEditing] = useState(false);
  const [busy, setBusy] = useState<"save" | "remove" | "verify" | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [verifyResult, setVerifyResult] = useState<DeepSeekVerifyResult | null>(null);

  const refresh = useCallback(async () => {
    try {
      setStatus(await api.get<DeepSeekKeyStatus>("/config/deepseek-key"));
      setLoadError(null);
    } catch (e) {
      setLoadError(errorText(e));
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const save = async () => {
    const value = draft.trim();
    if (!value) return; // the button is disabled too; this guard keeps the key out of a blank PUT
    setBusy("save");
    setNotice(null);
    setVerifyResult(null);
    try {
      const next = await api.put<DeepSeekKeyStatus>("/config/deepseek-key", { api_key: value });
      setStatus(next);
      setDraft(""); // the key lives in component state for the save round-trip only
      setEditing(false);
      setNotice("Saved. The key is stored on the backend and used by the agent runtime.");
    } catch (e) {
      setNotice(`Could not save the key. ${errorText(e)}`);
    } finally {
      setBusy(null);
    }
  };

  const remove = async () => {
    setBusy("remove");
    setNotice(null);
    setVerifyResult(null);
    try {
      const next = await api.delete<DeepSeekKeyStatus>("/config/deepseek-key");
      setStatus(next);
      setDraft("");
      setEditing(false);
      setNotice(
        next.configured
          ? "Stored key removed. The environment key is still active."
          : "Stored key removed. No DeepSeek key is configured now.",
      );
    } catch (e) {
      setNotice(`Could not remove the stored key. ${errorText(e)}`);
    } finally {
      setBusy(null);
    }
  };

  const verify = async () => {
    const candidate = draft.trim();
    setBusy("verify");
    setNotice(null);
    setVerifyResult(null);
    try {
      const result = await api.post<DeepSeekVerifyResult>(
        "/config/deepseek-key/verify",
        candidate ? { api_key: candidate } : {},
      );
      setVerifyResult(result);
    } catch (e) {
      // The probe could not even be requested — that is a real failure and is shown as one.
      setVerifyResult({ ok: false, detail: `Could not run the verification. ${errorText(e)}` });
    } finally {
      setBusy(null);
    }
  };

  const configured = status?.configured ?? false;
  const isStored = status?.source === "stored";
  const showInput = status !== null && (editing || !isStored);

  return (
    <div className="bg-gray-800 rounded-lg p-5 space-y-4" data-testid="deepseek-key-section">
      <div className="flex items-start justify-between gap-3">
        <div>
          <h2 className="text-white font-semibold">AI model (DeepSeek)</h2>
          <p className="text-xs text-gray-400 mt-1">
            The agents call DeepSeek with this key. It is stored on the backend only, never in
            this browser, and the API never returns it — not even in an error.
          </p>
        </div>
        {status && (
          <span
            data-testid="deepseek-source"
            className={`border rounded px-2 py-0.5 text-xs whitespace-nowrap ${SOURCE_BADGE[status.source]}`}
          >
            {SOURCE_LABEL[status.source]}
          </span>
        )}
      </div>

      {loadError && (
        <p className="text-sm text-red-400" role="alert">
          Could not load the key status. {loadError}
        </p>
      )}

      {status && (
        <div className="space-y-3">
          <div className="flex items-center gap-3">
            <span className="text-sm text-gray-400">Current key</span>
            <code
              data-testid="deepseek-masked"
              className="text-sm text-gray-200 bg-gray-900 border border-gray-700 rounded px-2 py-1"
            >
              {configured ? status.masked : "…"}
            </code>
          </div>

          {showInput && (
            <label className="block">
              <span className="block text-sm text-gray-400 mb-1">
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
                className="w-full bg-gray-700 text-white rounded px-3 py-2 text-sm border border-gray-600 focus:border-indigo-500 focus:outline-none"
              />
            </label>
          )}

          <div className="flex flex-wrap gap-2">
            {showInput && (
              <button
                onClick={save}
                disabled={busy !== null || draft.trim() === ""}
                className="px-4 py-2 bg-indigo-600 hover:bg-indigo-700 disabled:opacity-50 text-white rounded-md text-sm"
              >
                {busy === "save" ? "Saving…" : "Save key"}
              </button>
            )}
            {isStored && !editing && (
              <button
                onClick={() => {
                  setEditing(true);
                  setNotice(null);
                }}
                disabled={busy !== null}
                className="px-4 py-2 bg-gray-700 hover:bg-gray-600 disabled:opacity-50 text-white rounded-md text-sm"
              >
                Change
              </button>
            )}
            {editing && (
              <button
                onClick={() => {
                  setEditing(false);
                  setDraft("");
                }}
                disabled={busy !== null}
                className="px-4 py-2 bg-gray-700 hover:bg-gray-600 disabled:opacity-50 text-white rounded-md text-sm"
              >
                Cancel
              </button>
            )}
            {(configured || draft.trim() !== "") && (
              <button
                onClick={verify}
                disabled={busy !== null}
                className="px-4 py-2 bg-gray-700 hover:bg-gray-600 disabled:opacity-50 text-white rounded-md text-sm"
              >
                {busy === "verify" ? "Verifying…" : "Verify"}
              </button>
            )}
            {isStored && (
              <button
                onClick={remove}
                disabled={busy !== null}
                className="px-4 py-2 bg-red-900/60 hover:bg-red-900 disabled:opacity-50 text-red-200 rounded-md text-sm"
              >
                {busy === "remove" ? "Removing…" : "Remove"}
              </button>
            )}
          </div>
        </div>
      )}

      {notice && (
        <p role="status" className="text-sm text-gray-300">
          {notice}
        </p>
      )}

      {verifyResult && (
        <p
          role="status"
          data-testid="deepseek-verify-result"
          className={`text-sm ${verifyResult.ok ? "text-green-400" : "text-red-400"}`}
        >
          {verifyResult.ok ? "Verified — " : "Not verified — "}
          {verifyResult.detail}
        </p>
      )}
    </div>
  );
}
